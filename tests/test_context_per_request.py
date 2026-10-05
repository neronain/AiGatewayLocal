"""context ต่อคำขอ · จำนวน slot · ก้อนรวม (pool) — สามตัวเลขที่ต้องไม่ถูกเอามาปนกัน

เกตเวย์ประกาศตัวเลขเดียวต่อโมเดล: `limits.context_tokens` = token ที่ **คำขอเดียว**
ใช้ได้ (prompt + คำตอบ) · backend แต่ละชนิดตั้งคนละแบบ:

    llama.cpp   --ctx-size เป็นก้อนรวม หารเท่ากันด้วย --parallel
    vLLM        --max-model-len เป็นเพดานต่อคำขอ · ก้อนรวมเป็นงบหน่วยความจำแยกต่างหาก
    TensorFold  --context ต่อ stream · ก้อนรวม = parallel × context

ตรวจ 2026-10-05 พบว่าเกตเวย์ปล่อยให้สองฝั่งไม่ตรงกันได้หลายทาง และเมื่อไม่ตรง
ผู้ใช้เห็นเป็น 502 กับ backend ที่ถูกตีว่าล่ม ทั้งที่มันแค่ปฏิเสธ prompt ที่ยาวเกิน
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.core.capability import validate_context_budget
from app.core.errors import ErrorCode, GatewayError
from app.core.modeltest import probe_backend
from app.core.multimodal import (
    RequestProfile,
    profile_anthropic_request,
    profile_openai_request,
)
from app.core.tokens import estimate_prompt_tokens
from app.registry.schema import VisionPolicy
from app.registry.store import load_snapshot
from app.upstream import client as upstream

DIRECT = "http://dgx03:8000"  # coding, per config/models/coding.yaml
SPARE = "http://dgx-spare:8000"
POLICY = VisionPolicy()

LLAMACPP_OVERFLOW = {
    "error": {
        "code": 400,
        "type": "exceed_context_size_error",
        "message": "the request exceeds the available context size, try increasing it",
        "n_prompt_tokens": 60123,
        "n_ctx": 32768,
    }
}
VLLM_OVERFLOW = {
    "error": {
        "message": "This model's maximum context length is 262144 tokens. However, you "
                   "requested 300000 tokens (291808 in the messages, 8192 in the completion).",
        "type": "BadRequestError",
        "code": 400,
    }
}
TENSORFOLD_OVERFLOW = {
    "error": {
        "message": "This server's maximum context length is 262144 tokens; the prompt has "
                   "270001 tokens and requests 8192 reply tokens, which exceeds the context "
                   "window.",
        "code": "context_length_exceeded",
    }
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot(Path(__file__).resolve().parent.parent / "config")


# ---------------------------------------------------------------------------
# 1. งานของ tool คือ prompt — ต้องถูกนับ
# ---------------------------------------------------------------------------
def test_anthropic_tool_results_count_towards_the_context_check(snapshot):
    """Claude Code ส่งไฟล์และผลคำสั่งมาใน tool_result — เดิมนับเป็น 0 อักขระ

    คำขอ 1.77 ล้านอักขระเคยถูกประมาณเป็น 290 token แล้วผ่านด่านของโมเดล 131,072
    """
    file_text = "x = compute(alpha, beta)\n" * 70_000          # ~1.75M chars
    body = {
        "model": "muse-local",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": file_text},
            ]},
        ],
    }
    profile = profile_anthropic_request(body, POLICY)
    assert profile.text_chars >= len(file_text)

    model = snapshot.get("muse-local")          # 131,072 per request
    with pytest.raises(GatewayError) as caught:
        validate_context_budget(model, profile, 1024)
    assert caught.value.code == ErrorCode.CONTEXT_LENGTH_EXCEEDED


def test_anthropic_tool_result_blocks_tool_input_and_tool_definitions_are_counted():
    nested = "ผลลัพธ์ของคำสั่ง " * 500
    arguments = {"command": "grep -rn pattern " + "src/ " * 400}
    tools = [{"name": f"tool_{i}", "description": "does a thing " * 40,
              "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}}}
             for i in range(20)]
    bare = profile_anthropic_request(
        {"messages": [{"role": "user", "content": "hi"}]}, POLICY)
    full = profile_anthropic_request({
        "tools": tools,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "Bash", "input": arguments}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t",
                 "content": [{"type": "text", "text": nested}]}]},
        ],
    }, POLICY)
    added = full.text_chars - bare.text_chars
    assert added >= len(nested) + len(arguments["command"]) + 20 * 40 * len("does a thing ")
    thai = len(nested.replace(" ", ""))
    assert full.text_wide_chars >= thai, "ไทยใน tool_result ต้องนับเป็นอักขระกว้าง"


def test_openai_tool_definitions_and_tool_call_arguments_are_counted():
    arguments = '{"path": "' + "a/" * 3000 + '"}'
    tools = [{"type": "function", "function": {
        "name": "read", "description": "reads a file " * 300, "parameters": {"type": "object"}}}]
    bare = profile_openai_request({"messages": [{"role": "user", "content": "hi"}]}, POLICY)
    full = profile_openai_request({
        "tools": tools,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read", "arguments": arguments}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        ],
    }, POLICY)
    assert full.text_chars - bare.text_chars >= len(arguments) + 300 * len("reads a file ")


# ---------------------------------------------------------------------------
# 2. ใกล้เต็มหน้าต่าง: ต้องไม่ขอคำตอบเต็มเพดานไปด้วย
# ---------------------------------------------------------------------------
def test_a_prompt_estimated_just_over_the_window_is_not_sent_with_a_full_max_tokens(snapshot):
    """ระหว่าง 100% ถึง 115% ของหน้าต่าง เกตเวย์ปล่อยผ่าน (ค่าประมาณอาจเกินจริง)

    เดิมส่ง max_tokens เต็ม 8192 ไปด้วย — backend ที่ตรวจ prompt + max_tokens ปฏิเสธแน่นอน
    """
    model = snapshot.get("muse-local")
    window = model.spec.limits.context_tokens
    profile = RequestProfile()
    profile.add_text("word " * int(window * 1.05 * 4 / 5))     # ~105% ของหน้าต่าง
    estimate = estimate_prompt_tokens(profile, model.spec.wide_chars_per_token)
    assert window < estimate < window * 1.15, "เทสต้องอยู่ในช่วงที่ปล่อยผ่าน"

    assert validate_context_budget(model, profile, 8192) <= 256


# ---------------------------------------------------------------------------
# 3. backend บอกว่า prompt ยาวเกิน — ผู้ใช้ต้องได้คำนั้น ไม่ใช่ "502 เซิร์ฟเวอร์ปฏิเสธ"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [LLAMACPP_OVERFLOW, VLLM_OVERFLOW, TENSORFOLD_OVERFLOW],
                         ids=["llama.cpp", "vllm", "tensorfold"])
def test_a_backend_context_overflow_becomes_context_length_exceeded(snapshot, payload):
    import json

    endpoint = snapshot.get("coding").spec.endpoints[0]
    error = upstream.upstream_error(endpoint, 400, json.dumps(payload))
    assert error.code == ErrorCode.CONTEXT_LENGTH_EXCEEDED
    assert error.http_status == 400
    assert payload["error"]["message"][:60] in error.message, "ตัวเลขของ backend ต้องไปถึงผู้ใช้"


def test_an_ordinary_400_is_still_an_upstream_error(snapshot):
    endpoint = snapshot.get("coding").spec.endpoints[0]
    error = upstream.upstream_error(endpoint, 400, '{"error": {"message": "bad tool schema"}}')
    assert error.code == ErrorCode.UPSTREAM_ERROR


@respx.mock
def test_the_client_sees_400_context_length_exceeded_end_to_end(client, member_key):
    respx.post(f"{DIRECT}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json=VLLM_OVERFLOW))
    response = client.post(
        "/v1/chat/completions", headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400, response.text
    assert "maximum context length is 262144" in response.text


# ---------------------------------------------------------------------------
# 4. backend ที่ปฏิเสธคำขอ ไม่ใช่ backend ที่ล่ม
# ---------------------------------------------------------------------------
@respx.mock
def test_repeated_context_overflows_do_not_take_a_healthy_backend_out_of_rotation(
        client, member_key):
    """เดิม 400 สามครั้งติดกันทำให้ endpoint ถูกตี unhealthy — คนอื่นทั้งหมดใช้ไม่ได้ตาม"""
    respx.post(f"{DIRECT}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json=LLAMACPP_OVERFLOW))
    headers = auth(client.admin_key)

    for _ in range(6):                      # เพดานตั้งต้นคือ 3 ครั้งติดกัน
        reply = client.post(
            "/v1/chat/completions", headers=auth(member_key),
            json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})
        assert reply.status_code == 400

    health = client.get("/v1/health/endpoints", headers=headers).json()["data"]
    state = next(v for k, v in health.items() if k.startswith("coding:"))
    assert state["healthy"] is True, state


@respx.mock
def test_repeated_500s_still_mark_the_backend_unhealthy(client, member_key):
    """อีกด้านของข้อบน — ต้องไม่กลายเป็นว่าไม่มีอะไรทำให้ unhealthy ได้เลย"""
    respx.post(f"{DIRECT}/v1/chat/completions").mock(return_value=httpx.Response(500, text="boom"))
    for _ in range(6):
        client.post(
            "/v1/chat/completions", headers=auth(member_key),
            json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})
    health = client.get(
        "/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"]
    state = next(v for k, v in health.items() if k.startswith("coding:"))
    assert state["healthy"] is False, state


# ---------------------------------------------------------------------------
# 5. probe: llama.cpp บอกค่าต่อ slot และจำนวน slot — เอาทั้งสองมาใช้
# ---------------------------------------------------------------------------
def _llamacpp(base: str, *, n_ctx: int, slots: int) -> None:
    respx.get(f"{base}/v1/models").mock(return_value=httpx.Response(
        200, json={"data": [{"id": "m", "meta": {"n_ctx_train": 262144}}]}))
    respx.get(f"{base}/props").mock(return_value=httpx.Response(
        200, json={"default_generation_settings": {"n_ctx": n_ctx}, "total_slots": slots}))
    respx.post(f"{base}/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}))
    respx.post(f"{base}/v1/messages").mock(return_value=httpx.Response(404))


@pytest.mark.anyio
@respx.mock
async def test_the_probe_reads_per_slot_context_and_slot_count_from_llamacpp():
    """--ctx-size 131072 --parallel 4 → คำขอเดียวได้ 32,768 ไม่ใช่ 131,072 และไม่ใช่ 262,144"""
    base = "http://backend:8000"
    _llamacpp(base, n_ctx=32768, slots=4)
    result = await probe_backend(base, "m")
    assert result.context_tokens == 32768
    assert result.slots == 4
    assert any("per request" in note and "131,072" in note for note in result.notes)


@pytest.mark.anyio
@respx.mock
async def test_a_vllm_backend_reports_no_slot_count():
    base = "http://backend:8000"
    respx.get(f"{base}/v1/models").mock(return_value=httpx.Response(
        200, json={"data": [{"id": "m", "max_model_len": 131072}]}))
    respx.get(f"{base}/props").mock(return_value=httpx.Response(404))
    respx.post(f"{base}/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}))
    respx.post(f"{base}/v1/messages").mock(return_value=httpx.Response(404))
    result = await probe_backend(base, "m")
    assert result.context_tokens == 131072
    assert result.slots is None


# ---------------------------------------------------------------------------
# 6. ทะเบียนประกาศเกินที่ backend ให้จริง — ต้องขึ้นเป็น drift ไม่ใช่ "consistent"
# ---------------------------------------------------------------------------
@respx.mock
def test_advice_flags_a_declared_context_larger_than_the_backend_gives_one_request(client):
    """`coding` ประกาศ 262,144 และ max_concurrency 16 · backend ให้ 32,768 กับ 4 slot"""
    _llamacpp(DIRECT, n_ctx=32768, slots=4)
    body = client.get("/admin/models/coding/advice", headers=auth(client.admin_key)).json()
    backend = body["backends"][0]
    assert backend["context_tokens"] == 32768 and backend["slots"] == 4
    drift = {row["capability"]: row for row in backend["drift"]}
    assert drift["context_tokens (per request)"]["declared"] == 262144
    assert drift["context_tokens (per request)"]["measured"] == 32768
    assert drift["max_concurrency (backend slots)"]["declared"] == 16
    assert drift["max_concurrency (backend slots)"]["measured"] == 4
    assert body["summary"]["verdict"] != "consistent"


@respx.mock
def test_advice_does_not_flag_a_context_the_backend_can_actually_serve(client):
    respx.get(f"{DIRECT}/v1/models").mock(return_value=httpx.Response(
        200, json={"data": [{"id": "m", "max_model_len": 262144}]}))
    respx.get(f"{DIRECT}/props").mock(return_value=httpx.Response(404))
    respx.post(f"{DIRECT}/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}))
    respx.post(f"{DIRECT}/v1/messages").mock(return_value=httpx.Response(404))
    body = client.get("/admin/models/coding/advice", headers=auth(client.admin_key)).json()
    names = [row["capability"] for row in body["backends"][0]["drift"]]
    assert not any(name.startswith(("context_tokens", "max_concurrency")) for name in names)


# ---------------------------------------------------------------------------
# 7. อัตรา tokenizer ที่วัดไว้ต้องรอดการกด Save จากคอนโซล
# ---------------------------------------------------------------------------
def test_the_measured_tokenizer_rate_is_returned_to_the_console(client):
    """คอนโซลประกอบ spec ใหม่จากที่ GET คืนมา — ฟิลด์ที่ไม่ถูกคืนคือฟิลด์ที่หายตอน Save"""
    models = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    coding = next(m for m in models if m["alias"] == "coding")
    assert coding["wide_chars_per_token"] == 1.89


def test_saving_what_the_console_received_keeps_the_tokenizer_rate(writable_config, client):
    """เดินเส้นทางเดียวกับปุ่ม Save: GET → ประกอบเอกสาร → POST → อ่าน YAML ที่เขียนลงดิสก์"""
    import yaml

    headers = auth(client.admin_key)
    got = next(m for m in client.get("/admin/models", headers=headers).json()["data"]
               if m["alias"] == "coding")
    document = {
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": "coding", "display_name": "renamed only",
                     "description": got["description"], "visibility": got["visibility"],
                     "tags": got["tags"]},
        "spec": {
            "upstream_model": got["upstream_model"], "purpose": got["purpose"],
            "limits": got["limits"], "modalities": got["modalities"],
            "capabilities": got["capabilities"], "protocols": got["protocols"],
            "wide_chars_per_token": got["wide_chars_per_token"],
            "endpoints": [{k: v for k, v in e.items() if k != "health" and v is not None}
                          for e in got["endpoints"]],
            "enabled": True,
        },
    }
    saved = client.post("/admin/models", headers=headers, json=document)
    assert saved.status_code in (200, 201), saved.text
    on_disk = yaml.safe_load((writable_config / "models" / "coding.yaml").read_text())
    assert on_disk["spec"]["wide_chars_per_token"] == 1.89


# ---------------------------------------------------------------------------
# 8. หน้าเว็บ: รันฟังก์ชันของคอนโซลจริงใน node — ผู้ใช้ทำงานผ่านหน้าเว็บ ไม่ใช่ API
# ---------------------------------------------------------------------------
APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"


def _js_function(name: str) -> str:
    import re

    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    end = re.compile(r"^}\n", re.M).search(source, start).end()
    return source[start:end]


def _run_console(body: str, *, context: str = "262144", wide_rate: str = "null") -> dict:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    script = f"""
const fields = {{
  'm-alias': {{value: 'coding'}}, 'm-name': {{value: 'Coder'}}, 'm-desc': {{value: ''}},
  'm-visibility': {{value: 'member'}}, 'm-upstream': {{value: 'org/model'}},
  'm-ctx': {{value: '{context}'}}, 'm-out': {{value: '8192'}},
}};
const $ = (id) => fields[id] || (fields[id] = {{value: '', checked: false}});
const FORM_PURPOSES = ['general', 'coding'];
const state = {{cache: {{editingWideRate: {wide_rate}}}}};
const readEndpoints = () => [{{name: 'a', base_url: 'http://a:8000'}}];
const readFallback = () => [];
{_js_function("editorValues")}
{_js_function("editorProblems")}
{body}
"""
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_console_save_sends_back_the_tokenizer_rate_it_was_given():
    """ฟอร์มไม่มีช่องให้ค่านี้ — เดิมกด Save (แม้แค่แก้ชื่อ) แล้วค่าที่วัดไว้หายจาก YAML"""
    document = _run_console("console.log(JSON.stringify(editorValues()));", wide_rate="1.89")
    assert document["spec"]["wide_chars_per_token"] == 1.89
    assert document["spec"]["limits"]["context_tokens"] == 262144


def test_console_save_does_not_invent_a_tokenizer_rate():
    document = _run_console("console.log(JSON.stringify(editorValues()));")
    assert "wide_chars_per_token" not in document["spec"]


def test_console_refuses_to_save_a_model_with_no_context_value():
    """ช่องว่างเคยถูกเติม 131072 มาให้ก่อน แล้วค่าที่ Detect วัดได้ก็ไม่เคยถูกใช้"""
    problems = _run_console("console.log(JSON.stringify(editorProblems()));", context="")
    assert [p["field"] for p in problems] == ["m-ctx"]
    assert _run_console("console.log(JSON.stringify(editorProblems()));") == []
