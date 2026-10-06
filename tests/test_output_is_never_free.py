"""ส่งเนื้อหาออกไปแล้ว = ไม่มีวันบันทึก output เป็น 0

ตรวจพบ 2026-10-06: เมื่อ backend ไม่ได้รายงาน usage `resolve_usage(profile, None)` ตอบ
`output_tokens=0` ตายตัว ทั้งที่เกตเวย์เป็นคนส่งทุก delta ออกไปเอง · usage chunk คือสิ่ง
*สุดท้าย* ที่ backend ส่ง อะไรก็ตามที่จบ stream ก่อนถึงตรงนั้นจึงได้ output ฟรีทั้งก้อน:

* ผู้เรียกตัดสายก่อน chunk สุดท้าย — แถวยังบอก `status=success http_status=200` ด้วย ·
  coding agent ที่ยกเลิกคำขอทุกครั้งที่ผู้ใช้พิมพ์ต่อ ทำแบบนี้ตลอดเวลาโดยไม่ได้ตั้งใจโกง
* สายไป backend ขาดกลางทาง — `aborted/502` output 0
* คำตอบที่ไม่ได้ stream ซึ่ง backend ตอบ 200 โดยไม่มีบล็อก `usage` — ส่งต่อ 2,000 อักขระ
  แถวบอก 0 estimated

ตอนนี้ประมาณจากสิ่งที่ส่งต่อไปแล้ว ด้วยตัวประมาณและอัตราของโมเดลเดียวกับฝั่ง input และ
ติดป้าย `estimated`
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.tokens import OutputMeter, resolve_usage
from tests.realistic_backends import (
    CODING,
    DONE,
    MUSE,
    ROLE,
    STOP,
    SURFACE_NAMES,
    auth,
    chunk,
    hang_up_on,
    quota_used,
    read_stream,
    request_for,
    sse,
    streaming,
    terminal_error,
    usage_chunk,
    usage_rows,
    words,
)


def _slow_words(count: int, pause: float = 0.01) -> list:
    steps: list = []
    for piece in words(count):
        steps += [piece, pause]
    return steps


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_hanging_up_before_the_end_is_aborted_and_still_charged(client, member_key, surface):
    """ผู้เรียกรับไปแล้วหลายสิบคำแล้วตัดสาย — backend ยังไม่ทันส่ง usage chunk"""
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=lambda request: streaming(
        ROLE, *_slow_words(400), STOP, usage_chunk(11, 400), DONE))

    seen = hang_up_on(client, member_key, surface, after_frames=30)
    assert seen["status"] == 200 and seen["frames"] >= 30, seen

    row = usage_rows(client)[-1]
    assert (row["status"], row["http_status"], row["error_code"]) == (
        "aborted", 499, "CLIENT_CLOSED_REQUEST"), "ตัดสายกลางทางไม่ใช่ success/200"
    assert row["output"] >= 20, f"ส่งไปแล้วอย่างน้อย 20 คำ แต่บันทึก {row['output']}"
    assert row["output"] < 400 * 4, "คิดเท่าที่ส่งไป ไม่ใช่ทั้งคำตอบที่ยังไม่ได้เขียน"
    assert row["accounting"] == "estimated"
    assert row["input"] > 0, "ค่าประมาณ input ยังอยู่"
    used = quota_used(client)
    assert used["requests"] == 1 and used["output_tokens"] == row["output"]


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_backend_that_drops_mid_stream_does_not_make_the_output_free(
    client, member_key, surface
):
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=lambda request: streaming(
        ROLE, *words(30), httpx.ReadError("connection reset by peer")))

    _response, events = read_stream(client, member_key, surface)

    assert terminal_error(surface, events) is not None
    row = usage_rows(client)[-1]
    assert row["status"] == "error" and row["output"] >= 30 and row["accounting"] == "estimated"
    assert quota_used(client)["output_tokens"] == row["output"]


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_backend_that_ignores_the_usage_request_is_estimated_not_zero(
    client, member_key, surface
):
    """stream จบปกติแต่ไม่มี usage chunk เลย (backend ที่ไม่รู้จัก include_usage)

    ผู้เรียกต้องได้ตัวเลขชุดเดียวกับที่ถูกบันทึก — ไม่ใช่ถูกคิด N แต่ถูกบอกว่า 0
    """
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=lambda request: streaming(
        ROLE, *words(40), STOP, DONE))

    _response, events = read_stream(client, member_key, surface)

    row = usage_rows(client)[-1]
    assert row["status"] == "success" and row["accounting"] == "estimated"
    assert row["output"] >= 40
    if surface == "messages":
        told = next(p for e, p in events if e == "message_delta")["usage"]
        assert told == {"input_tokens": row["input"], "output_tokens": row["output"]}
    elif surface == "responses":
        told = next(p for e, p in events if e == "response.completed")["response"]["usage"]
        assert (told["input_tokens"], told["output_tokens"]) == (row["input"], row["output"])


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_complete_reply_without_a_usage_block_is_estimated(client, member_key, surface):
    reply = {"id": "c", "object": "chat.completion", "model": "up",
             "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "word " * 400}}]}
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply))

    path, body = request_for(surface)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 200, response.text
    row = usage_rows(client)[-1]
    assert row["accounting"] == "estimated" and row["status"] == "success"
    assert 400 <= row["output"] <= 600, row  # 2,000 อักขระละติน ~ 500 token
    told = response.json()["usage"]
    assert (told.get("completion_tokens") or told.get("output_tokens")) == row["output"]
    assert quota_used(client)["output_tokens"] == row["output"]


@respx.mock
def test_a_native_anthropic_stream_cut_short_is_not_billed_at_its_placeholder(
    client, member_key
):
    """llama.cpp พูด Anthropic เอง: message_start มี `output_tokens: 1` เป็นค่าตั้งต้น

    ตัวเลขจริงมาใน message_delta ตอนจบ · สายขาดก่อนถึงตรงนั้นแล้วเอา 1 ไปบันทึก = ฟรีเหมือนเดิม
    """
    def event(name: str, payload: dict) -> bytes:
        import json
        return f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n".encode()

    steps = [
        event("message_start", {"message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "up",
            "content": [], "usage": {"input_tokens": 900, "output_tokens": 1}}}),
        event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        *[event("content_block_delta", {"index": 0, "delta": {
            "type": "text_delta", "text": f"word{i} "}}) for i in range(30)],
        httpx.RemoteProtocolError("peer closed connection"),
    ]
    respx.post(f"{MUSE}/v1/messages").mock(side_effect=lambda request: streaming(*steps))

    _response, events = read_stream(client, member_key, "messages", model="muse-local")

    assert terminal_error("messages", events) is not None
    row = usage_rows(client)[-1]
    assert row["input"] == 900, "input ที่ backend วัดมาแล้วยังใช้"
    assert row["output"] >= 30 and row["accounting"] == "estimated", row


# ── ตัวประมาณเอง ───────────────────────────────────────────────────────────────
class _Profile:
    """โปรไฟล์คำขอที่เล็กที่สุดที่ resolve_usage อ่าน"""

    images: list = []
    text_chars = 400
    text_wide_chars = 0
    text_symbol_chars = 0
    pretokenized_tokens = 0


def test_the_meter_uses_the_same_rates_as_the_input_side():
    from app.core.tokens import estimate_chars

    thai = "สวัสดีครับ ที่นี่คือประเทศไทย " * 20
    code = 'def main():\n    return {"path": "src/main.py", "n": 42}\n' * 10
    for text, rate in ((thai, 1.89), (thai, 3.86), (code, None)):
        whole = OutputMeter()
        whole.add(text)
        pieces = OutputMeter()
        for index in range(0, len(text), 3):      # delta ของ stream จริงยาว 2–4 อักขระ
            pieces.add(text[index:index + 3])
        assert whole.tokens(rate) == pieces.tokens(rate) == estimate_chars(text, rate)


def test_two_characters_of_content_are_not_zero_tokens():
    meter = OutputMeter()
    meter.add("ok")
    assert meter.tokens() == 1
    assert OutputMeter().tokens() == 0, "ไม่ได้ส่งอะไร = 0 จริง ๆ"


def test_what_the_backend_measured_wins_over_the_estimate():
    meter = OutputMeter()
    meter.add("word " * 400)

    measured = resolve_usage(_Profile(), {"prompt_tokens": 50, "completion_tokens": 7},
                             relayed=meter)
    assert (measured.output_tokens, measured.accounting) == (7, "upstream")

    half = resolve_usage(_Profile(), {"prompt_tokens": 50, "completion_tokens": 0},
                         relayed=meter)
    assert half.input_tokens == 50 and half.output_tokens == meter.tokens()
    assert half.accounting == "estimated", "มีตัวเลขที่ไม่ได้วัดอยู่ในแถว = ติดป้ายให้เห็น"

    nothing = resolve_usage(_Profile(), None, relayed=meter)
    assert nothing.output_tokens == meter.tokens() and nothing.input_tokens == 100


def test_tool_calls_and_reasoning_count_as_output():
    from app.api.lifecycle import meter_chat

    meter = OutputMeter()
    meter_chat(meter, chunk({"reasoning_content": "let me think about this"}))
    thinking = meter.tokens()
    meter_chat(meter, chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {
        "name": "read_file", "arguments": '{"path": "src/main.py"}'}}]}))
    assert thinking > 0 and meter.tokens() > thinking
    assert sse(chunk({})) and meter.chars == len("let me think about this") + len(
        "read_file") + len('{"path": "src/main.py"}')
