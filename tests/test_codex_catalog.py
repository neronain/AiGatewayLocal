"""แค็ตตาล็อกโมเดลที่ Codex อ่านออก (`GET /v1/models?client_version=...`)

เจอจริง 2026-10-06 กับ Codex CLI 0.149.1: ทุกรอบที่ชี้ Codex มาที่เกตเวย์ มันพิมพ์

    failed to decode models response: missing field `models`

แล้วเปิดทุก thread ด้วย "Model metadata for `coding` not found" — คือใช้หน้าต่าง
272,000 token ที่มันเดาเองกับทุก alias · คำขอยังผ่าน จึงไม่มีอะไรฟ้อง จนโมเดลที่เล็ก
กว่านั้นถูกยัดจนเกตเวย์ปฏิเสธกลางงาน

เทสที่นี่ถามสามเรื่อง: client เดิมได้ของเดิมไหม · ค่าที่ส่งให้ Codex มาจากทะเบียนจริง
ไหม · และ Codex **ตัวจริง** decode ได้ไหม (ข้อหลังรันเมื่อเครื่องมี `codex` เท่านั้น)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "app" / "static" / "app.js"
VENDORED = REPO_ROOT / "app" / "vendor" / "codex"

CODEX = "/v1/models?client_version=0.149.1"

CHAT_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "backend-name",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def parse_toml(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:            # Python 3.10 — tomllib เข้ามาใน 3.11
        import tomli as tomllib
    return tomllib.loads(text)


def codex_models(client, key) -> dict[str, dict]:
    response = client.get(CODEX, headers=auth(key))
    assert response.status_code == 200, response.text
    return {m["slug"]: m for m in response.json()["models"]}


def edit_model(config: Path, alias: str, change) -> None:
    path = config / "models" / f"{alias}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document["spec"])
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")


def reload(client) -> None:
    response = client.post("/admin/registry/reload", headers=auth(client.admin_key))
    assert response.status_code == 200, response.text
    assert not response.json().get("errors"), response.text


# ── client เดิมต้องไม่รู้สึกอะไร ───────────────────────────────────────────────
def test_an_sdk_client_gets_exactly_what_it_got_before(client, member_key):
    """รายการของ Codex พก system prompt ~21KB ต่อโมเดล — ต้องไม่ไปถึงคนที่ไม่ได้ขอ"""
    body = client.get("/v1/models", headers=auth(member_key)).json()

    assert set(body) == {"object", "data"}
    assert body["object"] == "list"


def test_codex_gets_its_shape_next_to_the_openai_one_not_instead_of_it(client, member_key):
    plain = client.get("/v1/models", headers=auth(member_key)).json()
    for_codex = client.get(CODEX, headers=auth(member_key)).json()

    assert for_codex["data"] == plain["data"]
    assert for_codex["object"] == "list"
    assert [m["slug"] for m in for_codex["models"]] == ["coding"]


def test_the_catalogue_still_needs_a_key(client):
    assert client.get(CODEX).status_code == 401


# ── ค่ามาจากทะเบียน ไม่ใช่ค่าที่แต่งขึ้น ───────────────────────────────────────
def test_the_context_window_is_the_registry_limit(writable_config, client, member_key):
    """ตัวเลขนี้คือเหตุผลทั้งหมดของงาน — Codex ย่อบทสนทนาที่ 90% ของค่านี้"""
    assert codex_models(client, member_key)["coding"]["context_window"] == 262144

    edit_model(writable_config, "coding", lambda spec: spec["limits"].update(context_tokens=65536))
    reload(client)

    coding = codex_models(client, member_key)["coding"]
    assert coding["context_window"] == 65536
    assert coding["max_context_window"] == 65536, (
        "ผู้ใช้ตั้ง model_context_window เกินของจริงได้ ถ้าเพดานนี้ไม่ตาม"
    )


def test_image_input_is_offered_only_where_the_gateway_would_accept_it(
    writable_config, client, member_key
):
    def speak_responses(spec):
        spec["protocols"]["responses"] = True

    edit_model(writable_config, "gemma-vision", speak_responses)
    reload(client)

    models = codex_models(client, member_key)
    assert models["gemma-vision"]["input_modalities"] == ["text", "image"]
    assert models["coding"]["input_modalities"] == ["text"]


@respx.mock
def test_every_model_offered_to_codex_takes_a_codex_request_and_the_rest_refuse(
    writable_config, client, member_key
):
    """เสนอโมเดลที่จะถูกปฏิเสธ = error มาถึงหลังพิมพ์ prompt เสร็จ

    Codex แนบ tools มาทุกคำขอ · alias ที่เปิด responses แต่เรียก tool ไม่ได้ จึงใช้กับ
    Codex ไม่ได้เลย แม้ `protocols` จะบอกว่าพูด responses
    """
    def responses_with_tools(spec):
        spec["protocols"]["responses"] = True

    def responses_without_tools(spec):
        spec["protocols"]["responses"] = True
        spec["capabilities"]["tools"] = False

    edit_model(writable_config, "gemma-vision", responses_with_tools)
    edit_model(writable_config, "muse-local", responses_without_tools)
    reload(client)
    respx.post(url__regex=r"http://dgx0\d:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=CHAT_REPLY)
    )

    offered = set(codex_models(client, member_key))
    everything = {
        m["id"] for m in client.get("/v1/models", headers=auth(member_key)).json()["data"]
    }
    assert offered == {"coding", "gemma-vision"}
    assert everything - offered == {"muse-local"}

    def as_codex(alias):
        return client.post(
            "/v1/responses",
            headers=auth(member_key),
            json={
                "model": alias,
                "input": "what is 1+1",
                "instructions": "You are a coding agent.",
                "tools": [
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "run",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
        )

    for alias in sorted(offered):
        assert as_codex(alias).status_code == 200, alias
    for alias in sorted(everything - offered):
        assert as_codex(alias).status_code == 400, alias


def test_a_key_scoped_to_other_models_is_not_offered_this_one(client):
    """กติกาเดียวกับที่กั้นตัวคำขอ — ไม่ใช่กติกาชุดที่สองสำหรับ Codex"""
    admin = auth(client.admin_key)
    person = client.post("/admin/users", headers=admin, json={"external_id": "s1"}).json()
    key = client.post(
        "/admin/api-keys",
        headers=admin,
        json={"user_id": person["id"], "name": "k", "models": ["muse-local"]},
    ).json()["api_key"]

    assert codex_models(client, key) == {}


def test_the_upstream_repo_name_does_not_leak_through_the_new_shape(client, member_key):
    body = client.get(CODEX, headers=auth(member_key)).text

    assert "Qwen3-Coder" not in body
    assert "dgx03" not in body


def test_coding_models_come_first_because_codex_defaults_to_the_first(
    writable_config, client, member_key
):
    """ไม่ระบุ -m แล้ว Codex หยิบตัวที่ priority ต่ำสุด · `a-...` มาก่อน `coding` ตามตัวอักษร"""
    source = writable_config / "models" / "muse-local.yaml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    document["metadata"]["alias"] = "a-general"
    document["spec"]["purpose"] = ["general"]
    document["spec"]["protocols"]["responses"] = True
    source.unlink()
    (writable_config / "models" / "a-general.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    reload(client)

    models = codex_models(client, member_key)

    assert models["coding"]["priority"] < models["a-general"]["priority"]


# ── system prompt ─────────────────────────────────────────────────────────────
def test_every_entry_carries_the_prompt_codex_was_already_sending(client, member_key):
    """รายการเดียวที่ไม่มี instructions ทำให้ Codex decode ไม่ผ่านทั้งคำตอบ"""
    prompt = (VENDORED / "prompt.md").read_text(encoding="utf-8")

    for entry in codex_models(client, member_key).values():
        assert entry["base_instructions"] == prompt
    assert prompt.startswith("You are a coding agent running in the Codex CLI")


def test_the_vendored_prompt_travels_with_its_licence():
    """prompt เป็น Apache-2.0 ของ OpenAI ไม่ใช่ MIT ของเรา · `app/` ถูกก๊อปไปทั้งโฟลเดอร์
    ตอนติดตั้ง ใบอนุญาตจึงต้องอยู่ข้างไฟล์ ไม่ใช่ที่รากของ repo ซึ่งไม่ได้ไปด้วย"""
    assert "Apache License" in (VENDORED / "LICENSE").read_text(encoding="utf-8")
    assert "OpenAI" in (VENDORED / "NOTICE").read_text(encoding="utf-8")


# ── Codex ตัวจริง ─────────────────────────────────────────────────────────────
def _codex_reads(catalogue: dict, tmp_path: Path) -> dict:
    """ให้ deserializer ของ Codex เองอ่านแค็ตตาล็อก — ออฟไลน์ ไม่แตะ ~/.codex

    `codex debug models` โหลด `model_catalog_json` ด้วยชนิดเดียวกับที่ใช้ decode คำตอบ
    ของ `/models` (`ModelsResponse`) จึงล้มด้วยเหตุเดียวกันถ้ารูปผิด
    """
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("ไม่มี codex บนเครื่องนี้")
    path = tmp_path / "litegate-models.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")
    home = tmp_path / "codex-home"
    home.mkdir()
    done = subprocess.run(
        [codex, "debug", "models", "-c", f"model_catalog_json={json.dumps(str(path))}"],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
        env={"CODEX_HOME": str(home), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert done.returncode == 0, done.stderr
    return {m["slug"]: m for m in json.loads(done.stdout)["models"]}


def test_real_codex_decodes_the_response_as_served(client, member_key, tmp_path):
    served = client.get(CODEX, headers=auth(member_key)).json()

    understood = _codex_reads(served, tmp_path)

    assert set(understood) == {"coding"}
    assert understood["coding"]["context_window"] == 262144
    assert understood["coding"]["input_modalities"] == ["text"]


def test_real_codex_still_decodes_when_a_model_takes_video(
    writable_config, client, member_key, tmp_path
):
    """enum ของ Codex ไม่มี video · ค่าที่มันไม่รู้จักทำให้ล้มทั้งคำตอบ ไม่ใช่แค่รายการเดียว"""
    def takes_video(spec):
        spec["protocols"]["responses"] = True
        spec["modalities"]["input"] = ["text", "image", "video"]

    edit_model(writable_config, "gemma-vision", takes_video)
    reload(client)
    served = client.get(CODEX, headers=auth(member_key)).json()

    understood = _codex_reads(served, tmp_path)

    assert understood["gemma-vision"]["input_modalities"] == ["text", "image"]
    assert set(understood) == {"coding", "gemma-vision"}


# ── หน้า "Connect your tool" ──────────────────────────────────────────────────
def _console_codex_snippet(origin: str, key: str, model: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const claudeEnv = ")
    tools = source.index("const CT_TOOLS = {", start)
    end = re.compile(r"^};\n", re.M).search(source, tools).end()
    script = (
        source[start:end]
        + f"\nprocess.stdout.write(CT_TOOLS.codex.build({json.dumps(origin)}, "
        f"{json.dumps(key)}, {json.dumps(model)}));\n"
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_console_snippet_works_as_pasted_before_any_catalogue_exists():
    """Codex เปิดไม่ขึ้นเลยถ้า model_catalog_json ชี้ไฟล์ที่ไม่มี — ค่าตั้งต้นจึงต้องไม่ชี้"""
    config = parse_toml(_console_codex_snippet("https://gw.example", "lg_sk_x", "coding"))

    assert "model_catalog_json" not in config
    assert config["model"] == "coding"
    assert config["model_providers"]["litegate"]["base_url"] == "https://gw.example/v1"


def test_uncommenting_the_catalogue_line_sets_it_for_codex_not_for_the_provider():
    """ใต้ [model_providers.litegate] มันจะเป็นคีย์ของ provider ซึ่ง Codex เมิน"""
    snippet = _console_codex_snippet("https://gw.example", "lg_sk_x", "coding")
    enabled = snippet.replace("# model_catalog_json", "model_catalog_json")
    assert enabled != snippet

    config = parse_toml(enabled)

    assert config["model_catalog_json"] == "litegate-models.json"
    assert "model_catalog_json" not in config["model_providers"]["litegate"]


def test_the_download_command_in_the_snippet_fetches_a_catalogue(client, member_key):
    """คำสั่งที่บอกให้คนรัน ต้องได้ไฟล์ที่มี `models` จริง ไม่ใช่รูป OpenAI"""
    snippet = _console_codex_snippet("http://testserver", member_key, "coding")
    url = re.search(r'"(http://testserver/[^"]+)"', snippet).group(1)
    assert f"Bearer {member_key}" in snippet

    body = client.get(url.removeprefix("http://testserver"), headers=auth(member_key)).json()

    assert [m["slug"] for m in body["models"]] == ["coding"]
