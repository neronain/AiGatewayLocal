"""พารามิเตอร์ผิดชนิด/ผิดช่วง = 400 ที่บอกว่าฟิลด์ไหนผิด — ไม่ใช่ 500 ไม่ใช่สตรีมขาด

ตรวจ 2026-10-06 บน /v1/chat/completions:

    max_tokens: "100"        TypeError ในด่าน context            -> 500
    max_tokens: [1]          TypeError เดียวกัน                   -> 500
    max_tokens: -5           200 · backend ได้ max_tokens=1 โดยไม่มีใครบอก client
    max_tokens: 0            200 · backend ได้เพดานเต็ม 16,384
    stream_options: "yes"    200 เปิดสตรีมแล้ว AttributeError ข้างใน — สตรีมขาด และแถว
                             usage บันทึก "success" 0 token

เทสทุกตัวยิงผ่าน HTTP จริงแล้วยืนยันสามอย่างพร้อมกัน เพราะ "ได้ 400" อย่างเดียวไม่พอ:
ซองของ error ต้องเป็นของ surface นั้น · backend ต้องไม่ถูกเรียก · และต้อง **ไม่มีแถว usage**
(= ไม่มีการจองช่อง ไม่มีการหักโควตา — ทั้งสองอย่างเกิดหลังจุดที่มีแถว usage ได้เท่านั้น)
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

CODING = "http://dgx03:8000"       # coding · max_output_tokens 16,384 · เปิดทั้งสาม surface
CAP = 16_384

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
STREAM = (b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
          b"data: [DONE]\n\n")
MSG = [{"role": "user", "content": "hi"}]

PATHS = {"chat": "/v1/chat/completions", "messages": "/v1/messages",
         "responses": "/v1/responses"}
BASE = {"chat": {"messages": MSG}, "messages": {"messages": MSG}, "responses": {"input": "hi"}}
MAX = {"chat": "max_tokens", "messages": "max_tokens", "responses": "max_output_tokens"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def backend():
    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=STREAM,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=REPLY)

    with respx.mock:
        yield respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=answer)


def _post(client, key, surface: str, **extra):
    return client.post(PATHS[surface], headers=auth(key),
                       json={"model": "coding", **BASE[surface], **extra})


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def _usage_rows(client) -> int:
    from sqlalchemy import func, select

    from app.db.models import UsageLog
    from app.db.session import session_scope

    client.portal.call(client.app.state.services.usage.flush)

    async def count() -> int:
        async with session_scope() as session:
            return (await session.execute(select(func.count()).select_from(UsageLog))).scalar()

    return client.portal.call(count)


def _assert_clean_400(client, backend, response, surface: str, param: str) -> None:
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    if surface == "messages":
        # ซองของ Anthropic — SDK ของเขาอ่าน error.type ไม่ใช่ error.code
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "INVALID_REQUEST"
    else:
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "INVALID_REQUEST"
        assert body["error"]["param"] == param
    assert f"'{param}'" in body["error"]["message"], body["error"]["message"]
    assert not backend.called, "คำขอที่ผิดต้องไม่ไปถึง backend"
    assert _usage_rows(client) == 0, "ต้องปฏิเสธก่อนจะมีอะไรถูกบันทึกหรือหักโควตา"


# ---------------------------------------------------------------------------
# เพดาน output — ชื่อฟิลด์ต่างกันตาม surface กติกาเดียวกัน
# ---------------------------------------------------------------------------
BAD_MAX = ["100", [1], {"v": 1}, 12.5, -5, 0, True]


@pytest.mark.parametrize("surface", PATHS)
@pytest.mark.parametrize("value", BAD_MAX, ids=repr)
def test_a_bad_output_limit_is_a_400_on_every_surface(
        backend, client, member_key, surface, value):
    response = _post(client, member_key, surface, **{MAX[surface]: value})
    _assert_clean_400(client, backend, response, surface, MAX[surface])


@pytest.mark.parametrize("value", BAD_MAX, ids=repr)
def test_max_completion_tokens_is_held_to_the_same_rule(backend, client, member_key, value):
    response = _post(client, member_key, "chat", max_completion_tokens=value)
    _assert_clean_400(client, backend, response, "chat", "max_completion_tokens")


@pytest.mark.parametrize("surface", PATHS)
@pytest.mark.parametrize("value, sent", [(100, 100), (100.0, 100), (None, CAP), (10**9, CAP)],
                         ids=["int", "integral-float", "null", "over-the-cap"])
def test_a_good_output_limit_still_works(backend, client, member_key, surface, value, sent):
    """ตัวควบคุม — ด่านใหม่ต้องไม่ปฏิเสธของที่เคยใช้ได้ · 100.0 คือ 100 ที่ JSON เขียนอีกแบบ"""
    response = _post(client, member_key, surface, **{MAX[surface]: value})
    assert response.status_code == 200, response.text
    got = _sent(backend)["max_tokens"]
    assert got == sent and isinstance(got, int)


def test_max_completion_tokens_still_reaches_the_backend_as_max_tokens(
        backend, client, member_key):
    response = _post(client, member_key, "chat", max_completion_tokens=300)
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == 300


# ---------------------------------------------------------------------------
# sampling · stream · stop
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", PATHS)
@pytest.mark.parametrize("param, value", [
    ("temperature", "hot"), ("temperature", -0.1), ("temperature", 2.5),
    ("temperature", True), ("temperature", [0.5]),
    ("top_p", "0.9"), ("top_p", 1.5), ("top_p", -1),
    ("stream", "yes"), ("stream", 1), ("stream", "false"),
], ids=lambda v: repr(v))
def test_bad_sampling_and_stream_values_are_a_400(
        backend, client, member_key, surface, param, value):
    response = _post(client, member_key, surface, **{param: value})
    _assert_clean_400(client, backend, response, surface, param)


@pytest.mark.parametrize("surface", PATHS)
def test_ordinary_sampling_values_pass(backend, client, member_key, surface):
    response = _post(client, member_key, surface, temperature=0, top_p=1, stream=False)
    assert response.status_code == 200, response.text
    assert _sent(backend)["temperature"] == 0


@pytest.mark.parametrize("surface, param, value", [
    ("chat", "stop", 5), ("chat", "stop", ["a", 1]), ("chat", "stop", {"a": 1}),
    ("messages", "stop_sequences", "END"), ("messages", "stop_sequences", [1]),
    ("messages", "top_k", -1), ("messages", "top_k", "40"), ("messages", "top_k", 1.5),
    ("responses", "instructions", ["be brief"]),
], ids=lambda v: repr(v))
def test_bad_stop_and_friends_are_a_400(backend, client, member_key, surface, param, value):
    response = _post(client, member_key, surface, **{param: value})
    _assert_clean_400(client, backend, response, surface, param)


def test_stop_may_be_one_string_or_a_list(backend, client, member_key):
    for stop in ("END", ["END", "STOP"]):
        response = _post(client, member_key, "chat", stop=stop)
        assert response.status_code == 200, response.text
        assert _sent(backend)["stop"] == stop


# ---------------------------------------------------------------------------
# stream_options — เคสที่เคยพังหลังตอบ 200 ไปแล้ว
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value, param", [
    ("yes", "stream_options"), ([], "stream_options"), (True, "stream_options"),
    ({"include_usage": "yes"}, "stream_options.include_usage"),
], ids=lambda v: repr(v))
def test_bad_stream_options_are_refused_before_the_stream_opens(
        backend, client, member_key, value, param):
    with client.stream("POST", PATHS["chat"], headers=auth(member_key), json={
            "model": "coding", "messages": MSG, "stream": True,
            "stream_options": value}) as response:
        response.read()
    # 400 เป็น JSON ทั้งก้อน — ไม่ใช่ 200 text/event-stream ที่ขาดกลางทาง
    _assert_clean_400(client, backend, response, "chat", param)


def test_good_stream_options_still_stream(backend, client, member_key):
    with client.stream("POST", PATHS["chat"], headers=auth(member_key), json={
            "model": "coding", "messages": MSG, "stream": True,
            "stream_options": {"include_usage": True}}) as response:
        body = response.read().decode()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert body.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# tools · tool_choice
# ---------------------------------------------------------------------------
FUNCTION = {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}


@pytest.mark.parametrize("surface, param, extra", [
    ("chat", "tools", {"tools": "read"}),
    ("chat", "tools", {"tools": {"name": "read"}}),
    ("chat", "tools[0]", {"tools": ["read"]}),
    ("chat", "tools[0].function", {"tools": [{"type": "function"}]}),
    ("chat", "tools[1].function.name", {"tools": [FUNCTION, {"type": "function",
                                                              "function": {"name": ""}}]}),
    ("chat", "tool_choice", {"tools": [FUNCTION], "tool_choice": 1}),
    ("chat", "tool_choice", {"tools": [FUNCTION], "tool_choice": ["read"]}),
    ("messages", "tools", {"tools": "read"}),
    ("messages", "tools[0]", {"tools": ["read"]}),
    ("messages", "tools[0].name", {"tools": [{"input_schema": {"type": "object"}}]}),
    ("messages", "tool_choice", {"tools": [{"name": "read", "input_schema": {}}],
                                 "tool_choice": "auto"}),
    ("messages", "tool_choice.type", {"tools": [{"name": "read", "input_schema": {}}],
                                      "tool_choice": {"name": "read"}}),
    ("responses", "tools", {"tools": "shell"}),
    ("responses", "tools[0]", {"tools": ["shell"]}),
    ("responses", "tools[0].name", {"tools": [{"type": "function"}]}),
    ("responses", "tool_choice", {"tool_choice": 3}),
], ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_tools_are_a_400(backend, client, member_key, surface, param, extra):
    response = _post(client, member_key, surface, **extra)
    _assert_clean_400(client, backend, response, surface, param)


@pytest.mark.parametrize("surface, extra", [
    ("chat", {"tools": [FUNCTION], "tool_choice": "auto"}),
    ("chat", {"tools": [FUNCTION],
              "tool_choice": {"type": "function", "function": {"name": "read"}}}),
    ("messages", {"tools": [{"name": "read", "description": "r",
                             "input_schema": {"type": "object"}}],
                  "tool_choice": {"type": "auto"}}),
    ("responses", {"tools": [{"type": "function", "name": "shell",
                              "parameters": {"type": "object"}}], "tool_choice": "auto"}),
], ids=["chat-auto", "chat-named", "messages", "responses"])
def test_well_formed_tools_pass(backend, client, member_key, surface, extra):
    response = _post(client, member_key, surface, **extra)
    assert response.status_code == 200, response.text
    assert _sent(backend)["tools"][0]["function"]["name"] in {"read", "shell"}


# ---------------------------------------------------------------------------
# n — หลายคำตอบในคำขอเดียวต้องอยู่ใต้เพดาน output เดียวกัน
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, -1, "2", 2.5, [2], True, 129], ids=repr)
def test_a_bad_n_is_a_400(backend, client, member_key, value):
    response = _post(client, member_key, "chat", n=value)
    _assert_clean_400(client, backend, response, "chat", "n")


def test_n_choices_share_one_output_cap(backend, client, member_key):
    """เดิม n=4 โดยไม่ระบุ max_tokens ส่ง max_tokens=16,384 *ต่อคำตอบ* = 65,536 ต่อคำขอ

    `limits.max_output_tokens` คือเพดานต่อคำขอที่แค็ตตาล็อกโชว์ และโควตาตรวจล่วงหน้าไม่ได้ ·
    เพดานที่คูณได้ด้วยฟิลด์เดียวไม่ใช่เพดาน
    """
    response = _post(client, member_key, "chat", n=4)
    assert response.status_code == 200, response.text
    sent = _sent(backend)
    assert sent["n"] == 4
    assert sent["max_tokens"] * sent["n"] <= CAP
    assert sent["max_tokens"] == CAP // 4


def test_n_does_not_lower_a_request_that_already_fits(backend, client, member_key):
    response = _post(client, member_key, "chat", n=4, max_tokens=100)
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == 100


def test_a_single_choice_is_untouched(backend, client, member_key):
    response = _post(client, member_key, "chat", n=1)
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == CAP
