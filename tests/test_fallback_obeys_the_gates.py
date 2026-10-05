"""โมเดลสำรองต้องผ่านด่านชุดเดียวกับโมเดลที่ขอ — และเพดานคำตอบต้องเป็นของตัวที่เสิร์ฟจริง

surface · capability · ขนาด context ถูกตรวจกับโมเดลที่ขอครั้งเดียวตอนต้นคำขอ · ตรวจ
2026-10-05 พบว่าเมื่อเครื่องของมันล่มหรือเต็มแล้วล้มไปโมเดลอื่น (`routing.fallback`)
**ไม่มีด่านไหนถูกตรวจซ้ำกับตัวสำรองเลย** มีแค่ `max_output_tokens` ที่ถูกเอามา min ทับ

ทุกเทสดูที่ของจริง: backend ของตัวสำรองถูกเรียกหรือไม่ และ payload ที่มันได้รับคืออะไร
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

CODING = "http://dgx03:8000"   # coding · 262,144 · max_output 16,384 · tools · anthropic
MUSE = "http://dgx01:8000"     # muse-local · 131,072 · max_output 8,192
GEMMA = "http://dgx02:8000"    # gemma-vision · protocols.anthropic: false
SPARE = "http://dgx-spare:8000"

REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-name",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}

# "word " = 5 อักขระ ≈ 1.25 token ตามตัวประมาณของเกตเวย์
LONG_200K = "word " * 160_000      # ~200,000 token: พอสำหรับ coding · เกิน muse-local แน่นอน
NEAR_128K = "word " * 102_400      # ~128,000 token: พอดีหน้าต่าง 131,072 เหลือ ~3,000


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _edit(config, alias: str, change) -> None:
    path = config / "models" / f"{alias}.yaml"
    document = yaml.safe_load(path.read_text())
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def _fallback(config, *aliases: str) -> None:
    _edit(config, "coding", lambda d: d["spec"].__setitem__(
        "routing", {"fallback": list(aliases)}))


@pytest.fixture
def coding_falls_back_to_muse(writable_config):
    _fallback(writable_config, "muse-local")
    return writable_config


def _coding_is_down():
    return respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused"))


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def chat(client, key, text: str, **extra):
    return client.post(
        "/v1/chat/completions", headers=auth(key),
        json={"model": "coding", "messages": [{"role": "user", "content": text}], **extra})


# ---------------------------------------------------------------------------
# 1. context: ตัวสำรองที่หน้าต่างแคบกว่า prompt ต้องไม่ถูกส่งไป
# ---------------------------------------------------------------------------
@respx.mock
def test_a_prompt_too_long_for_the_fallback_is_not_sent_to_it(
        coding_falls_back_to_muse, client, member_key):
    """prompt 200k: `coding` (262,144) รับได้ แต่เครื่องล่ม · `muse-local` (131,072) รับไม่ได้

    เดิมถูกส่งไป muse-local แล้ว backend ปฏิเสธ — ผู้ใช้ได้ 400 จากโมเดลที่เขาไม่ได้เลือก
    """
    _coding_is_down()
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))

    response = chat(client, member_key, LONG_200K)

    assert not muse.called, "ตัวสำรองรับ prompt นี้ไม่ไหว ต้องไม่ถูกเรียก"
    assert response.status_code >= 500, "ความจริงคือเครื่องของ coding ล่ม — ต้องได้คำนั้น"
    assert response.json()["error"]["code"] != "CONTEXT_LENGTH_EXCEEDED"


@respx.mock
def test_a_prompt_that_fits_the_fallback_still_falls_back(
        coding_falls_back_to_muse, client, member_key):
    """อีกด้านของข้อบน — ด่านใหม่ต้องไม่ทำให้ fallback เลิกทำงาน"""
    _coding_is_down()
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))

    response = chat(client, member_key, "hi")

    assert response.status_code == 200, response.text
    assert muse.called
    assert response.headers["x-litegate-served-by"] == "muse-local"


@pytest.fixture
def one_slot_coding_falls_back_to_muse(coding_falls_back_to_muse):
    _edit(coding_falls_back_to_muse, "coding", lambda d: d["spec"]["endpoints"][0].__setitem__(
        "max_concurrency", 1))
    return coding_falls_back_to_muse


@respx.mock
def test_the_same_check_applies_when_the_fallback_is_chosen_before_any_attempt(
        one_slot_coding_falls_back_to_muse, client, member_key):
    """fallback มีสองทางเข้า: ล้มกลางทาง (ข้อบน) กับ *ไม่มีเครื่องให้เลือกตั้งแต่แรก*

    ทางที่สองเกิดเมื่อทุกเครื่องของ alias เต็ม — จำลองด้วยคำขอที่ค้างอยู่บน coding 1 ช่อง
    แล้วยิงคำขอยาว 200k ซ้อนเข้าไประหว่างนั้น
    """
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))
    nested: list[httpx.Response] = []

    async def busy(request: httpx.Request) -> httpx.Response:
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as inner:
            nested.append(await inner.post(
                "/v1/chat/completions", headers=auth(member_key),
                json={"model": "coding",
                      "messages": [{"role": "user", "content": LONG_200K}]}))
        return httpx.Response(200, json=REPLY)

    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=busy)

    assert chat(client, member_key, "hi").status_code == 200
    assert not muse.called, "ตัวสำรองรับ prompt นี้ไม่ไหว ต้องไม่ถูกเรียก"
    assert nested[0].status_code == 429, nested[0].text


# ---------------------------------------------------------------------------
# 2. เพดานคำตอบต้องคิดใหม่กับหน้าต่างของตัวสำรอง
# ---------------------------------------------------------------------------
@respx.mock
def test_max_tokens_is_recomputed_against_the_fallback_window(
        coding_falls_back_to_muse, client, member_key):
    """prompt ~128k + ขอ 8,192: บน coding เหลือที่ถมเถ · บน muse-local เหลือ ~3,000

    เดิมส่ง 8,192 ที่ clamp ไว้กับ coding ไปให้ muse-local — 128k + 8,192 เกิน 131,072
    backend ที่ตรวจ prompt + max_tokens จึงปฏิเสธคำขอที่มันรับได้
    """
    _coding_is_down()
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))

    response = chat(client, member_key, NEAR_128K, max_tokens=8192)

    assert response.status_code == 200, response.text
    sent = _sent(muse)["max_tokens"]
    assert 256 <= sent <= 131_072 - 128_000, f"ส่ง max_tokens={sent} ให้หน้าต่าง 131,072"


@respx.mock
def test_the_fallback_models_own_output_cap_applies(
        coding_falls_back_to_muse, client, member_key):
    _coding_is_down()
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))
    chat(client, member_key, "hi", max_tokens=16_000)      # coding ให้ได้ถึง 16,384
    assert _sent(muse)["max_tokens"] == 8192               # muse-local ให้ได้ 8,192


# ---------------------------------------------------------------------------
# 3. capability กับ surface: ด่านเดียวกับที่ตัวแรกผ่านมา
# ---------------------------------------------------------------------------
TOOLS = [{"type": "function", "function": {
    "name": "read", "description": "read a file", "parameters": {"type": "object"}}}]


@pytest.fixture
def coding_falls_back_to_a_model_without_tools(writable_config):
    _edit(writable_config, "muse-local", lambda d: d["spec"]["capabilities"].__setitem__(
        "tools", False))
    _fallback(writable_config, "muse-local")
    return writable_config


@respx.mock
def test_a_tool_request_does_not_fall_back_to_a_model_without_tools(
        coding_falls_back_to_a_model_without_tools, client, member_key):
    """ขอ `muse-local` ตรง ๆ พร้อม tools ได้ 400 จากเกตเวย์ — ผ่าน fallback ต้องไม่ต่างกัน"""
    _coding_is_down()
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))

    response = chat(client, member_key, "read main.py", tools=TOOLS)
    assert not muse.called, "โมเดลที่ประกาศ tools=false ได้รับคำขอที่มี tools"
    assert response.status_code >= 500

    assert chat(client, member_key, "hi").status_code == 200, "ไม่มี tools ก็ยัง fallback ได้"
    assert muse.called


@pytest.fixture
def coding_falls_back_to_a_model_without_the_anthropic_surface(writable_config):
    _fallback(writable_config, "gemma-vision")
    return writable_config


@respx.mock
def test_an_anthropic_request_does_not_fall_back_to_a_model_that_closed_that_surface(
        coding_falls_back_to_a_model_without_the_anthropic_surface, client, member_key):
    """แอดมินปิด `protocols.anthropic` ของ gemma-vision ไว้ — fallback ต้องไม่เปิดให้เอง"""
    _coding_is_down()
    gemma = respx.post(f"{GEMMA}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))
    body = {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}

    response = client.post("/v1/messages", headers=auth(member_key),
                           json={"model": "coding", **body})
    assert not gemma.called
    assert response.status_code >= 500

    # surface ที่ gemma-vision เปิดไว้ยัง fallback ได้ตามเดิม
    assert chat(client, member_key, "hi").status_code == 200
    assert gemma.called


# ---------------------------------------------------------------------------
# 4. failover ข้ามเครื่องบน surface ที่ต้องแปล
# ---------------------------------------------------------------------------
@pytest.fixture
def two_machines(writable_config):
    """`coding` บนสองเครื่อง ทั้งคู่พูดแต่ OpenAI — /v1/messages กับ /v1/responses ต้องแปล"""
    def add(document):
        first = document["spec"]["endpoints"][0]
        spare = {**first, "name": "spare", "base_url": SPARE, "priority": 90}
        document["spec"]["endpoints"] = [first, spare]
    _edit(writable_config, "coding", add)
    return writable_config


@respx.mock
@pytest.mark.parametrize("path, body", [
    ("/v1/messages", {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"input": "hi"}),
], ids=["messages", "responses"])
def test_translated_surfaces_fail_over_to_the_second_machine(
        two_machines, client, member_key, path, body):
    """chat ล้มไปเครื่องสำรองได้ · สอง surface นี้ไม่เคยได้ — เพราะตอน failover ไปถามหา
    เครื่องที่ *พูด protocol นั้นเอง* ซึ่งโมเดลที่เสิร์ฟผ่านตัวแปลไม่มีสักเครื่อง
    """
    _coding_is_down()
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY))

    response = client.post(path, headers=auth(member_key), json={"model": "coding", **body})

    assert response.status_code == 200, response.text
    assert spare.called
    assert response.headers["x-litegate-endpoint"] == "spare"
    assert "dgx03" in response.headers["x-litegate-failed-over"]
