"""`limits.max_output_tokens` เป็นเพดาน — ทั้งสาม surface ต้องบังคับเหมือนกัน

แค็ตตาล็อกโชว์ "Max output N" ต่อโมเดล · ตรวจ 2026-10-05 พบว่า /v1/messages กับ
/v1/responses ส่งเพดานนี้ให้ backend ทุกคำขอ แต่ /v1/chat/completions ส่งเฉพาะเมื่อ client
ใส่ `max_tokens` มาเอง — ไม่ใส่ = backend เขียนได้จนเต็ม context

**ตัดสินแล้ว: ส่งเพดานเสมอทั้งสามทาง** · เพดานที่ข้ามได้ด้วยการไม่ส่งฟิลด์ไม่ใช่เพดาน:
คำขอเดียวกินโควตา output ทั้งก้อนได้ (โควตาตรวจก่อน บันทึกทีหลัง) และถือ slot ของ
llama.cpp ไว้เป็นนาที · client ที่ไม่ใส่ `max_tokens` คือส่วนใหญ่ (Open WebUI · n8n · SDK
ค่าตั้งต้น) ไม่ใช่ส่วนน้อย

ราคาที่จ่าย: โมเดล reasoning ใช้ token คิดจากเพดานเดียวกัน — `max_output_tokens` ของมัน
ต้องตั้งให้พอทั้งคิดและตอบ (ดู tests/test_reasoning_is_not_dropped.py)
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

CODING = "http://dgx03:8000"       # coding · max_output_tokens 16,384
CAP = 16_384

CHAT_REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-name",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}

MESSAGES = [{"role": "user", "content": "hi"}]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def backend():
    with respx.mock:
        yield respx.post(f"{CODING}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=CHAT_REPLY))


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def _post(client, key, path: str, **body):
    response = client.post(path, headers=auth(key), json={"model": "coding", **body})
    assert response.status_code == 200, response.text
    return response


# ---------------------------------------------------------------------------
# client ไม่ได้ขอ — เพดานของโมเดลถูกส่งไปทั้งสามทาง
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {"messages": MESSAGES}),
    ("/v1/messages", {"messages": MESSAGES}),
    ("/v1/responses", {"input": "hi"}),
], ids=["chat", "messages", "responses"])
def test_the_models_cap_is_sent_when_the_client_names_none(
        backend, client, member_key, path, body):
    _post(client, member_key, path, **body)
    assert _sent(backend)["max_tokens"] == CAP


def test_streaming_chat_gets_the_cap_too(backend, client, member_key):
    """สตรีมเป็นทางที่ client ไม่ใส่ max_tokens บ่อยที่สุด และเป็นทางที่ถือ slot นานที่สุด"""
    backend.mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'))
    with client.stream("POST", "/v1/chat/completions", headers=auth(member_key),
                       json={"model": "coding", "messages": MESSAGES, "stream": True}) as reply:
        assert reply.status_code == 200
        reply.read()
    assert _sent(backend)["max_tokens"] == CAP


# ---------------------------------------------------------------------------
# client ขอ — ได้ตามขอ แต่ไม่เกินเพดาน
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {"messages": MESSAGES, "max_tokens": 100}),
    ("/v1/chat/completions", {"messages": MESSAGES, "max_completion_tokens": 100}),
    ("/v1/messages", {"messages": MESSAGES, "max_tokens": 100}),
    ("/v1/responses", {"input": "hi", "max_output_tokens": 100}),
], ids=["chat", "chat-max_completion_tokens", "messages", "responses"])
def test_a_smaller_request_is_honoured(backend, client, member_key, path, body):
    _post(client, member_key, path, **body)
    sent = _sent(backend)
    assert sent["max_tokens"] == 100
    assert "max_completion_tokens" not in sent, "ส่งสองชื่อไปพร้อมกัน backend เลือกเองว่าจะเชื่อตัวไหน"


@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {"messages": MESSAGES, "max_tokens": 999_999}),
    ("/v1/messages", {"messages": MESSAGES, "max_tokens": 999_999}),
    ("/v1/responses", {"input": "hi", "max_output_tokens": 999_999}),
], ids=["chat", "messages", "responses"])
def test_a_larger_request_is_capped(backend, client, member_key, path, body):
    _post(client, member_key, path, **body)
    assert _sent(backend)["max_tokens"] == CAP


# ---------------------------------------------------------------------------
# ใกล้เต็มหน้าต่าง: เพดานที่ใส่ให้เองต้องไม่ทำให้คำขอที่เคยผ่านถูกปฏิเสธ
# ---------------------------------------------------------------------------
def test_an_injected_cap_leaves_room_for_the_prompt(backend, client, member_key):
    """prompt ~255k บนหน้าต่าง 262,144 · เดิมไม่ส่ง max_tokens backend จึงให้เท่าที่เหลือ

    ส่ง 16,384 เต็มเพดานไปตอนนี้ = 255k + 16k เกินหน้าต่าง และ backend ปฏิเสธคำขอที่มันเคยรับ
    """
    prompt = "word " * 204_000                       # ~255,000 token ตามตัวประมาณ
    _post(client, member_key, "/v1/chat/completions",
          messages=[{"role": "user", "content": prompt}])
    assert 256 <= _sent(backend)["max_tokens"] <= 262_144 - 255_000
