"""ความคิดของโมเดล reasoning ต้องไม่หายระหว่างแปล OpenAI → Anthropic

โมเดล reasoning หลัง backend แบบ OpenAI คืนความคิดใน `reasoning_content` (vLLM รุ่นใหม่ใช้
`reasoning`) แยกจาก `content` · ตรวจ 2026-10-05 พบว่าตัวแปลของ /v1/messages ทิ้งฟิลด์นี้
เสมอ — โมเดลที่ใช้งบหมดไปกับการคิดจึงตอบ Claude Code ว่า

    content: [{"type": "text", "text": ""}]   stop_reason: "max_tokens"   HTTP 200

คือคำตอบว่างที่คิดเงินเต็ม และไม่มีอะไรบอกว่าโมเดลทำงานไปแล้วทั้งก้อน

**ตัดสินแล้ว: คืนเป็น block `thinking` เมื่อผู้เรียกขอ thinking มา และเฉพาะเมื่อขอ** ·
API ของ Anthropic เองคืน block นี้เฉพาะเมื่อคำขอเปิด thinking · โค้ด client ที่เขียนว่า
`message.content[0].text` ถูกต้องตามสัญญานั้น และจะพังถ้าเราแทรก block ที่เขาไม่ได้ขอไว้หน้าสุด
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

CODING = "http://dgx03:8000"       # coding · endpoint พูดแต่ OpenAI — /v1/messages ต้องแปล

THINKING = {"type": "enabled", "budget_tokens": 1024}
ASK = {"model": "coding", "max_tokens": 64, "messages": [{"role": "user", "content": "2+2?"}]}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def backend_says(message: dict, finish: str = "stop") -> None:
    respx.post(f"{CODING}/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-name",
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 64, "total_tokens": 73},
    }))


def messages(client, key, **extra) -> dict:
    response = client.post("/v1/messages", headers=auth(key), json={**ASK, **extra})
    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-protocol"] == "anthropic-via-openai"
    return response.json()


# ---------------------------------------------------------------------------
# ไม่สตรีม
# ---------------------------------------------------------------------------
@respx.mock
def test_a_reply_that_was_all_thinking_is_not_returned_as_an_empty_string(client, member_key):
    """เคสที่ทำให้ต้องมีไฟล์นี้: งบหมดระหว่างคิด — เดิมผู้เรียกได้ข้อความว่างอย่างเดียว"""
    backend_says({"content": "", "reasoning_content": "The user asks 2+2. Adding"},
                 finish="length")
    body = messages(client, member_key, thinking=THINKING)
    assert body["content"] == [
        {"type": "thinking", "thinking": "The user asks 2+2. Adding", "signature": ""}]
    assert body["stop_reason"] == "max_tokens"


@respx.mock
def test_thinking_comes_before_the_answer(client, member_key):
    backend_says({"content": "4", "reasoning_content": "2+2 is 4."})
    body = messages(client, member_key, thinking=THINKING)
    assert [block["type"] for block in body["content"]] == ["thinking", "text"]
    assert body["content"][1]["text"] == "4"


@respx.mock
def test_the_newer_vllm_field_name_is_read_too(client, member_key):
    """vLLM เปลี่ยนชื่อจาก `reasoning_content` เป็น `reasoning` — ดูชื่อเดียวคือครึ่งฟลีตหาย"""
    backend_says({"content": "4", "reasoning": "2+2 is 4."})
    body = messages(client, member_key, thinking=THINKING)
    assert body["content"][0] == {"type": "thinking", "thinking": "2+2 is 4.", "signature": ""}


@respx.mock
@pytest.mark.parametrize("extra", [{}, {"thinking": {"type": "disabled"}}],
                         ids=["not-asked", "disabled"])
def test_a_caller_who_did_not_ask_for_thinking_gets_none(client, member_key, extra):
    """`content[0].text` ต้องยังใช้ได้กับ client ที่ไม่ได้ขอ thinking — ตามสัญญาของ Anthropic"""
    backend_says({"content": "4", "reasoning_content": "2+2 is 4."})
    body = messages(client, member_key, **extra)
    assert body["content"] == [{"type": "text", "text": "4"}]


@respx.mock
def test_thinking_precedes_a_tool_call(client, member_key):
    backend_says({"content": None, "reasoning_content": "I should read the file.",
                  "tool_calls": [{"id": "call_1", "type": "function",
                                  "function": {"name": "read", "arguments": '{"path": "a.py"}'}}]},
                 finish="tool_calls")
    body = messages(client, member_key, thinking=THINKING, tools=[
        {"name": "read", "description": "read a file", "input_schema": {"type": "object"}}])
    assert [block["type"] for block in body["content"]] == ["thinking", "tool_use"]
    assert body["stop_reason"] == "tool_use"


# ---------------------------------------------------------------------------
# สตรีม — ประกอบ event กลับเป็น block แบบที่ SDK ของ Anthropic ทำ
# ---------------------------------------------------------------------------
def _sse(*deltas: dict, finish: str = "stop") -> bytes:
    chunks = [{"choices": [{"index": 0, "delta": delta}]} for delta in deltas]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    chunks.append({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 20}})
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines + ["data: [DONE]\n\n"]).encode()


def stream(client, key, upstream: bytes, **extra) -> tuple[list[dict], str]:
    """ยิงสตรีมแล้วประกอบ block จาก event ตามลำดับ index · คืน (blocks, stop_reason)

    ประกอบเข้มเท่าที่ SDK ทำ: delta ต้องมาหลัง start ของ index เดียวกันและก่อน stop ·
    index ต้องเริ่มที่ 0 และไม่ข้าม — client จริงพังเงียบ ๆ กับลำดับที่ผิดแบบนั้น
    """
    respx.post(f"{CODING}/v1/chat/completions").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=upstream))
    with client.stream("POST", "/v1/messages", headers=auth(key),
                       json={**ASK, "stream": True, **extra}) as response:
        assert response.status_code == 200
        raw = response.read().decode()

    blocks: list[dict] = []
    open_blocks: set[int] = set()
    stop_reason = ""
    for frame in raw.strip().split("\n\n"):
        data = next(ln[5:].strip() for ln in frame.splitlines() if ln.startswith("data:"))
        event = json.loads(data)
        kind = event["type"]
        if kind == "content_block_start":
            assert event["index"] == len(blocks), "index ต้องเรียงต่อกันจาก 0"
            blocks.append(dict(event["content_block"]))
            open_blocks.add(event["index"])
        elif kind == "content_block_delta":
            assert event["index"] in open_blocks, "delta มานอกช่วงที่ block เปิดอยู่"
            delta, block = event["delta"], blocks[event["index"]]
            if delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "input_json_delta":
                block["input_json"] = block.get("input_json", "") + delta["partial_json"]
        elif kind == "content_block_stop":
            open_blocks.remove(event["index"])
        elif kind == "message_delta":
            stop_reason = event["delta"]["stop_reason"]
    assert not open_blocks, "มี block ที่ไม่ถูกปิด"
    return blocks, stop_reason


@respx.mock
def test_streamed_thinking_arrives_as_its_own_block_before_the_text(client, member_key):
    upstream = _sse({"reasoning_content": "2+2 "}, {"reasoning_content": "is 4."},
                    {"content": "The answer "}, {"content": "is 4."})
    blocks, stop = stream(client, member_key, upstream, thinking=THINKING)
    assert blocks == [
        {"type": "thinking", "thinking": "2+2 is 4.", "signature": ""},
        {"type": "text", "text": "The answer is 4."},
    ]
    assert stop == "end_turn"


@respx.mock
def test_a_stream_that_was_all_thinking_still_shows_the_thinking(client, member_key):
    upstream = _sse({"reasoning": "Let me think about "}, {"reasoning": "this"}, finish="length")
    blocks, stop = stream(client, member_key, upstream, thinking=THINKING)
    assert blocks == [
        {"type": "thinking", "thinking": "Let me think about this", "signature": ""}]
    assert stop == "max_tokens"


@respx.mock
def test_a_stream_for_a_caller_who_did_not_ask_carries_no_thinking(client, member_key):
    upstream = _sse({"reasoning_content": "2+2 is 4."}, {"content": "4"})
    blocks, _ = stream(client, member_key, upstream)
    assert blocks == [{"type": "text", "text": "4"}]


@respx.mock
def test_streamed_thinking_is_closed_before_a_tool_call_opens(client, member_key):
    upstream = _sse(
        {"reasoning_content": "I should read it."},
        {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                         "function": {"name": "read", "arguments": ""}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a.py"}'}}]},
        finish="tool_calls")
    blocks, stop = stream(client, member_key, upstream, thinking=THINKING, tools=[
        {"name": "read", "description": "read a file", "input_schema": {"type": "object"}}])
    assert [block["type"] for block in blocks] == ["thinking", "tool_use"]
    assert blocks[1]["input_json"] == '{"path": "a.py"}'
    assert stop == "tool_use"
