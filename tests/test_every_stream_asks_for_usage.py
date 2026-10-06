"""stream ทุกเส้นที่ไปถึง backend ต้องขอ usage — ไม่งั้น output ถูกคิดเป็น 0

ตรวจพบ 2026-10-06: มีแค่ /v1/chat/completions ที่ใส่ `stream_options.include_usage` ·
ตัวแปลของ /v1/messages กับ /v1/responses คัดลอก `stream` ไปแต่ไม่ขอ usage · vLLM ส่ง usage
chunk ปิดท้าย **เฉพาะเมื่อถูกขอ** ผลบนรูปที่ส่งมอบจริง (config/models/coding.yaml: vllm ·
`anthropic: false`) คือทางของ Claude Code และ Codex ทั้งเส้น:

    usage row    output_tokens=0 … accounting=estimated status=success
    โควตา        output_tokens: 0
    ผู้เรียก      "usage":{"output_tokens":0}

ชุดเทสเดิมเขียวเพราะ mock แนบ usage chunk มาเสมอไม่ว่าจะถูกขอหรือไม่ · backend ในไฟล์นี้
(`VllmLike`) ส่งเฉพาะเมื่อถูกขอ เหมือนของจริง
"""

from __future__ import annotations

import pytest
import respx

from tests.realistic_backends import (
    CODING,
    SURFACE_NAMES,
    VllmLike,
    ended_normally,
    quota_used,
    read_stream,
    text_of,
    usage_rows,
)


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_streamed_output_is_measured_and_charged(client, member_key, surface):
    backend = VllmLike(pieces=40, prompt_tokens=1234, completion_tokens=321)
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=backend)

    response, events = read_stream(client, member_key, surface)

    assert response.status_code == 200
    assert "word39" in text_of(surface, events) and ended_normally(surface, events)
    assert backend.seen[0]["stream_options"] == {"include_usage": True}, (
        "backend ไม่ถูกขอ usage = มันจะไม่ส่งมา"
    )
    row = usage_rows(client)[-1]
    assert (row["input"], row["output"], row["accounting"], row["status"]) == (
        1234, 321, "upstream", "success"), row
    used = quota_used(client)
    assert (used["input_tokens"], used["output_tokens"]) == (1234, 321), used


@respx.mock
def test_the_anthropic_stream_tells_the_caller_its_real_input_size(client, member_key):
    """ครึ่งหลังของบั๊กเดียวกัน: ต่อให้ backend ส่ง usage มา ผู้เรียกแบบ stream ก็ไม่เคยรู้ขนาด input

    เดิม `message_start` บอก `input_tokens: 0` และ `message_delta` มีแค่ `output_tokens` ·
    backend ที่พูด chat completions บอกขนาด prompt ใน chunk สุดท้าย หลัง message_start ออกไป
    นานแล้ว ตัวเลขจริงจึงต้องไปใน `message_delta.usage` (API ของ Anthropic เองก็ใส่ไว้ที่นั่น
    และทั้ง SDK กับ Claude Code อ่านทับค่าจาก message_start) ส่วน message_start ได้ค่าประมาณ
    ของเกตเวย์แทน 0
    """
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=VllmLike(prompt_tokens=1234, completion_tokens=321))

    _response, events = read_stream(client, member_key, "messages")

    start = next(p for e, p in events if e == "message_start")["message"]["usage"]
    delta = next(p for e, p in events if e == "message_delta")["usage"]
    assert delta == {"input_tokens": 1234, "output_tokens": 321}
    assert start["input_tokens"] > 0, "ค่าประมาณของเกตเวย์ ไม่ใช่ 0"
    assert start["output_tokens"] == 0


@respx.mock
def test_the_responses_stream_completes_with_the_real_numbers(client, member_key):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=VllmLike(prompt_tokens=1234, completion_tokens=321))

    _response, events = read_stream(client, member_key, "responses")

    usage = next(p for e, p in events if e == "response.completed")["response"]["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (
        1234, 321, 1555)


@respx.mock
def test_asking_for_usage_does_not_clobber_what_the_caller_set(client, member_key):
    """`stream_options` ของผู้เรียกมีตัวเลือกอื่นได้ (vLLM: continuous_usage_stats) — ต้องไปถึง"""
    backend = VllmLike()
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=backend)

    read_stream(client, member_key, "chat", stream_options={"continuous_usage_stats": True})

    assert backend.seen[0]["stream_options"] == {
        "continuous_usage_stats": True, "include_usage": True}


@respx.mock
@pytest.mark.parametrize("asked", [False, True])
def test_the_usage_chunk_reaches_a_chat_caller_only_if_they_asked(client, member_key, asked):
    """เราขอจาก backend เสมอ แต่รูปของ stream ที่ผู้เรียกได้ต้องตรงกับที่เขาขอ"""
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=VllmLike())
    extra = {"stream_options": {"include_usage": True}} if asked else {}

    _response, events = read_stream(client, member_key, "chat", **extra)

    usage_chunks = [p for _, p in events if isinstance(p, dict) and p.get("usage")]
    assert len(usage_chunks) == (1 if asked else 0)
    assert usage_rows(client)[-1]["output"] == 321  # บันทึกจาก backend ทั้งสองกรณี
