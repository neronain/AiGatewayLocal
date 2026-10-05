"""ช่องของ backend เป็นของ *เครื่องที่เสิร์ฟ* ไม่ใช่ของชื่อที่สมาชิกเรียก

`max_concurrency` บอกว่า process ตัวหนึ่งรับได้กี่คำขอพร้อมกัน · ตรวจ 2026-10-05 พบว่า
ตัวนับถูกผูกกับสิ่งอื่นสามแบบ และทุกแบบจบที่เรื่องเดียวกัน — llama.cpp 1 slot ที่ตั้ง
`max_concurrency: 1` ได้รับ 2 คำขอ:

1. **นับใต้ alias ที่ขอ ไม่ใช่ตัวที่เสิร์ฟ** · `Router.select()` ดูความจุของโมเดลที่จะรัน
   แต่ `acquire`/`release`/`report_*` ใช้ `ctx.requested_alias` — ทราฟฟิกที่ถูก reroute
   หรือ fallback จึงได้ตัวนับอีกกองหนึ่ง และความล้มเหลวของมันไปลงบัญชีสุขภาพผิดเครื่อง
2. **alias สองตัวชี้ process เดียวกัน** · คีย์เดิมคือ `alias:ชื่อ endpoint` สอง alias จึง
   ไม่เคยเห็นกัน
3. **ใบจองใช้ `x-request-id` ที่ client ส่งมาเอง** · ส่งค่าเดิมซ้ำ = ใบจองใบเดียวกัน
   ทุกคำขอ ตัวนับไม่ขยับเลย

เทสทุกตัวในไฟล์นี้ยิงคำขอที่สอง **ระหว่างที่คำขอแรกยังค้างอยู่ที่ backend จริง ๆ**
(side effect ของ respx ยิงเข้าแอปตัวเดียวกันผ่าน ASGI) แล้วนับว่า backend ถูกเรียกกี่ครั้ง
"""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml

CODING = "http://dgx03:8000"   # coding · vLLM · max_concurrency 16
MUSE = "http://dgx01:8000"     # muse-local · llama.cpp · max_concurrency 1


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def reply(text: str = "ok") -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-name",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def _edit(config, alias: str, change) -> None:
    path = config / "models" / f"{alias}.yaml"
    document = yaml.safe_load(path.read_text())
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def _while_busy(client, backend: str, second: dict, headers: dict) -> tuple[list, list]:
    """ตั้ง backend ให้ *ระหว่างตอบคำขอแรก* ยิงคำขอ `second` เข้าเกตเวย์อีกหนึ่งตัว

    คืน (สถานะของคำขอที่สอง, รายการคำขอที่ backend ได้รับ) — ตัวหลังคือของจริงที่ต้องดู:
    backend 1 slot ต้องเห็นคำขอเดียว ไม่ว่าเกตเวย์จะรายงานตัวเลขอะไร
    """
    nested: list[int] = []
    arrived: list[httpx.Request] = []

    async def busy(request: httpx.Request) -> httpx.Response:
        arrived.append(request)
        if len(arrived) == 1:
            transport = httpx.ASGITransport(app=client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gw") as inner:
                answer = await inner.post("/v1/chat/completions", json=second, headers=headers)
            nested.append(answer.status_code)
        return httpx.Response(200, json=reply())

    respx.post(f"{backend}/v1/chat/completions").mock(side_effect=busy)
    return nested, arrived


SMALL = {"messages": [{"role": "user", "content": "hi"}]}


# ---------------------------------------------------------------------------
# 1. ทราฟฟิกที่ถูก reroute ต้องนับที่เครื่องซึ่งมันไปลงจริง
# ---------------------------------------------------------------------------
@pytest.fixture
def small_prompts_go_to_muse(writable_config):
    """`coding` ส่งงานจุกจิกให้ `muse-local` — llama.cpp 1 slot"""
    _edit(writable_config, "coding", lambda d: d["spec"].__setitem__(
        "routing", {"small_prompt": {"under_tokens": 2000, "target": "muse-local"}}))
    return writable_config


@respx.mock
def test_a_rerouted_request_occupies_the_slot_of_the_model_that_serves_it(
        small_prompts_go_to_muse, client, member_key):
    """ขอ `coding` → เสิร์ฟโดย `muse-local` · ระหว่างนั้นขอ `muse-local` ตรง ๆ อีกตัว

    เดิมตัวแรกจองใต้คีย์ `coding:dgx01` ตัวที่สองจองใต้ `muse-local:dgx01` — ผ่านทั้งคู่
    """
    nested, arrived = _while_busy(
        client, MUSE, {"model": "muse-local", **SMALL}, auth(member_key))

    first = client.post("/v1/chat/completions", headers=auth(member_key),
                        json={"model": "coding", **SMALL})

    assert first.status_code == 200, first.text
    assert first.headers["x-litegate-served-by"] == "muse-local", "เทสต้องผ่านกฎ routing จริง"
    assert nested == [429], "ช่องเดียวถูกใช้อยู่ — ตัวที่สองต้องถูกบอกให้รอ"
    assert len(arrived) == 1, f"backend 1 slot ได้รับ {len(arrived)} คำขอพร้อมกัน"


@respx.mock
def test_the_slot_is_free_again_once_the_rerouted_request_finishes(
        small_prompts_go_to_muse, client, member_key):
    """อีกด้านของข้อบน — ตัวนับที่ไม่ยอมคืนคือ backend ที่ "เต็ม" ไปตลอดกาล"""
    respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply()))
    for model in ("coding", "muse-local", "coding"):
        answer = client.post("/v1/chat/completions", headers=auth(member_key),
                             json={"model": model, **SMALL})
        assert answer.status_code == 200, answer.text
    health = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"]
    assert health["muse-local:dgx01"]["in_flight"] == 0


@respx.mock
def test_failures_of_rerouted_traffic_are_charged_to_the_machine_that_failed(
        small_prompts_go_to_muse, client, member_key):
    """เครื่องของ `muse-local` พัง — ต้องเป็น `muse-local` ที่ถูกตีว่า unhealthy

    เดิมความล้มเหลวไปลงบัญชี `coding:dgx01` ซึ่งไม่มีอยู่จริง · หน้า health จึงยังเขียวทั้งคู่
    และคำขอถัดไปก็ถูกส่งเข้าเครื่องที่พังต่อไปเรื่อย ๆ
    """
    respx.post(f"{MUSE}/v1/chat/completions").mock(return_value=httpx.Response(500, text="boom"))
    for _ in range(4):                       # เพดานตั้งต้นคือ 3 ครั้งติดกัน
        client.post("/v1/chat/completions", headers=auth(member_key),
                    json={"model": "coding", **SMALL})
    health = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"]
    assert health["muse-local:dgx01"]["healthy"] is False, health["muse-local:dgx01"]
    assert health["coding:dgx03"]["healthy"] is True, "เครื่องของ coding ไม่ได้ถูกแตะเลย"


# ---------------------------------------------------------------------------
# 2. fallback ระดับโมเดล: เหมือนกัน — นับที่ตัวสำรอง
# ---------------------------------------------------------------------------
@pytest.fixture
def coding_falls_back_to_muse(writable_config):
    _edit(writable_config, "coding", lambda d: d["spec"].__setitem__(
        "routing", {"fallback": ["muse-local"]}))
    return writable_config


@respx.mock
def test_a_fallback_request_occupies_the_slot_of_the_fallback_model(
        coding_falls_back_to_muse, client, member_key):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused"))
    nested, arrived = _while_busy(
        client, MUSE, {"model": "muse-local", **SMALL}, auth(member_key))

    first = client.post("/v1/chat/completions", headers=auth(member_key),
                        json={"model": "coding", **SMALL})

    assert first.status_code == 200, first.text
    assert first.headers["x-litegate-served-by"] == "muse-local"
    assert nested == [429]
    assert len(arrived) == 1


# ---------------------------------------------------------------------------
# 3. alias สองตัวบน process เดียวกัน ใช้ช่องชุดเดียวกัน
# ---------------------------------------------------------------------------
def _alias_of_muse(config, alias: str, **endpoint) -> None:
    """alias ใหม่ที่ชี้ server เดียวกับ muse-local — แบบที่คนตั้งชื่อ claude-* ให้ Claude Code

    ต้องเรียกจาก fixture ที่มาก่อน `client` เสมอ: แอปอ่านทะเบียนครั้งเดียวตอนเริ่ม
    """
    document = yaml.safe_load((config / "models" / "muse-local.yaml").read_text())
    document["metadata"]["alias"] = alias
    document["spec"]["endpoints"][0].update(endpoint)
    (config / "models" / f"{alias}.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


@pytest.fixture
def claude_local(writable_config):
    """ชื่อ endpoint ต่างกัน · base_url เขียนคนละแบบ — ยังเป็น process เดียวกัน"""
    _alias_of_muse(writable_config, "claude-local", name="spark", base_url=f"{MUSE}/v1/")
    return writable_config


@pytest.fixture
def other_model_same_server(writable_config):
    _alias_of_muse(writable_config, "other-model", name="spark", upstream_model="org/another")
    return writable_config


@respx.mock
def test_two_aliases_on_the_same_server_and_model_share_its_slots(
        claude_local, client, member_key):
    nested, arrived = _while_busy(
        client, MUSE, {"model": "claude-local", **SMALL}, auth(client.admin_key))

    first = client.post("/v1/chat/completions", headers=auth(client.admin_key),
                        json={"model": "muse-local", **SMALL})

    assert first.status_code == 200, first.text
    assert nested == [429]
    assert len(arrived) == 1, f"process เดียว 1 slot ได้รับ {len(arrived)} คำขอพร้อมกัน"


@respx.mock
def test_the_health_page_says_which_aliases_share_a_backend(claude_local, client):
    """ตัวเลข 1/1 ที่ขึ้นพร้อมกันสองแถวต้องมีคำอธิบาย ไม่งั้นอ่านเป็น "สองคำขอ" """
    health = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"]
    assert health["muse-local:dgx01"]["shares_slots_with"] == ["claude-local:spark"]
    assert health["claude-local:spark"]["shares_slots_with"] == ["muse-local:dgx01"]
    assert health["coding:dgx03"]["shares_slots_with"] == []


def test_the_console_health_table_shows_who_shares_the_slots(claude_local, client):
    """รันตัววาดตารางของคอนโซลจริงใน node ด้วยข้อมูลที่ API คืนมาจริง"""
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(
        encoding="utf-8")

    def function(name: str) -> str:
        start = source.index(f"function {name}(")
        return source[start:re.compile(r"^}\n", re.M).search(source, start).end()]

    report = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"]
    script = f"""
const table = {{innerHTML: ''}};
const $ = () => table;
const esc = (v) => String(v);
const num = (v) => String(v);
{function("sharedSlotsHint")}
{function("renderHealth")}
renderHealth({json.dumps(report)});
console.log(JSON.stringify(table.innerHTML.split('<tr>').slice(2)));
"""
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    rows = {re.search(r"<code>(.*?)</code>", row).group(1): row
            for row in json.loads(done.stdout)}
    assert "ใช้ช่องร่วมกับ claude-local:spark" in rows["muse-local"]
    assert "ใช้ช่องร่วมกับ muse-local:dgx01" in rows["claude-local"]
    assert "ใช้ช่องร่วมกับ" not in rows["coding"]


@respx.mock
def test_the_same_server_with_a_different_model_is_not_the_same_slots(
        other_model_same_server, client, member_key):
    """server เดียวเสิร์ฟหลายโมเดลได้ (Ollama · ผู้ให้บริการภายนอก) — คนละโมเดล คนละช่อง

    ถ้ารวมด้วย base_url อย่างเดียว alias ทุกตัวของผู้ให้บริการรายเดียวจะแย่งเพดานกองเดียว
    """
    nested, arrived = _while_busy(
        client, MUSE, {"model": "other-model", **SMALL}, auth(client.admin_key))

    first = client.post("/v1/chat/completions", headers=auth(client.admin_key),
                        json={"model": "muse-local", **SMALL})

    assert first.status_code == 200, first.text
    assert nested == [200]
    assert len(arrived) == 2


# ---------------------------------------------------------------------------
# 4. ใบจองต้องเป็นของเกตเวย์ ไม่ใช่ค่าที่ client เลือกเอง
# ---------------------------------------------------------------------------
@respx.mock
def test_reusing_an_x_request_id_does_not_reuse_the_lease(client, member_key):
    """`x-request-id` เดิมซ้ำ = ใบจองใบเดียวกันในที่เก็บที่ทุก worker ใช้ร่วมกัน

    ใบที่สองเขียนทับใบแรก ตัวนับจึงไม่ขยับ และ release ของตัวใดตัวหนึ่งคืนช่องของทุกตัว
    · ใน worker เดียวตัวนับในเครื่องยังช่วยบังไว้ จึงต้องดูที่ *สิ่งที่ถูกส่งให้ที่เก็บร่วม*
    ซึ่งเป็นด่านเดียวที่เหลือเมื่อคำขอสองตัวตกคนละ worker (production รัน 4 worker + Redis)
    """
    from app.core.inflight import LocalInFlightLimiter

    class Recording(LocalInFlightLimiter):
        def __init__(self) -> None:
            super().__init__()
            self.leases: list[str] = []

        async def acquire(self, key: str, limit: int, lease: str) -> bool:
            self.leases.append(lease)
            return await super().acquire(key, limit, lease)

    shared = Recording()
    client.app.state.services.router.set_limiter(shared)
    respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply()))
    same = {**auth(member_key), "x-request-id": "always-the-same"}

    for _ in range(2):
        answer = client.post("/v1/chat/completions", headers=same,
                             json={"model": "muse-local", **SMALL})
        assert answer.status_code == 200, answer.text
        assert answer.headers["x-request-id"] == "always-the-same", "ค่าที่ echo กลับยังเป็นของ client"

    assert len(set(shared.leases)) == 2, f"สองคำขอใช้ใบจองใบเดียวกัน: {shared.leases}"
