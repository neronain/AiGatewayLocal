"""When one machine goes down mid-conversation, the next one answers.

Health checks alone were never enough. A backend stays marked healthy for two
more strikes after its first failure, so the request that arrives while a box is
dying gets sent to the dying box and fails — and so do the next two. From the
member's side a machine going down did not degrade the service, it broke their
conversation, three times, before routing caught up.

The rule that keeps this honest is the streaming one: a retry is free while
nothing has reached the caller, and forbidden the moment something has, because
replaying from the top would show them the answer twice.
"""

from __future__ import annotations

import httpx
import pytest
import respx

DIRECT = "http://dgx03:8000"     # coding · priority 100
SPARE = "http://dgx-spare:8000"  # added below · priority 90

REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "whatever-the-backend-calls-it",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def two_machines(writable_config):
    """`coding` served by two backends, the spare at lower priority."""
    import yaml

    path = writable_config / "models" / "coding.yaml"
    document = yaml.safe_load(path.read_text())
    first = document["spec"]["endpoints"][0]
    spare = {**first, "name": "spare", "base_url": SPARE, "priority": 90}
    document["spec"]["endpoints"] = [first, spare]
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    return writable_config


def ask(client, key, **extra):
    return client.post(
        "/v1/chat/completions",
        headers=auth(key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}], **extra},
    )


@respx.mock
def test_a_refused_connection_is_answered_by_the_other_machine(two_machines, client, member_key):
    down = respx.post(f"{DIRECT}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    up = respx.post(f"{SPARE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    response = ask(client, member_key)

    assert response.status_code == 200, response.text
    assert down.called and up.called, "ต้องลองตัวแรกก่อน แล้วค่อยไปตัวสำรอง"
    assert response.headers["x-litegate-endpoint"] == "spare"
    assert "dgx03" in response.headers["x-litegate-failed-over"]


@respx.mock
def test_a_500_moves_on_but_a_400_does_not(two_machines, client, member_key):
    """4xx คือคำตัดสินของ backend ต่อ *คำขอ* — เครื่องอื่นก็ตอบเหมือนกัน ยิงซ้ำคือช้าเปล่า"""
    respx.post(f"{DIRECT}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    assert ask(client, member_key).status_code >= 400
    assert not spare.called, "400 ต้องไม่ทำให้เครื่องสำรองโดนยิงตาม"


@respx.mock
def test_a_500_does_move_on(two_machines, client, member_key):
    respx.post(f"{DIRECT}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="upstream exploded")
    )
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    response = ask(client, member_key)
    assert response.status_code == 200
    assert spare.called


@respx.mock
def test_both_down_reports_the_failure_rather_than_looping(two_machines, client, member_key):
    """ไม่มีเครื่องเหลือแล้วต้องหยุด · วนต่อคือ request ที่ไม่มีวันจบ"""
    first = respx.post(f"{DIRECT}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("down")
    )
    second = respx.post(f"{SPARE}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("down")
    )

    response = ask(client, member_key)

    assert response.status_code >= 500
    assert first.call_count == 1 and second.call_count == 1, "แต่ละเครื่องต้องถูกลองครั้งเดียว"


@respx.mock
def test_one_backend_is_tried_once_and_only_once(client, member_key):
    """`coding` ในคอนฟิกจริงมีเครื่องเดียว — ต้องไม่ยิงซ้ำที่เดิม"""
    only = respx.post(f"{DIRECT}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("down")
    )
    assert ask(client, member_key).status_code >= 500
    assert only.call_count == 1


def sse(*chunks: str) -> httpx.Response:
    body = "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


CHUNK = (
    '{"id":"1","object":"chat.completion.chunk","model":"m",'
    '"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}'
)


@respx.mock
def test_a_stream_that_fails_before_the_first_token_moves_on(
    two_machines, client, member_key
):
    respx.post(f"{DIRECT}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("down")
    )
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(return_value=sse(CHUNK))

    response = ask(client, member_key, stream=True)

    assert response.status_code == 200
    assert spare.called
    assert "hello" in response.text
    assert "error" not in response.text.lower()


@respx.mock
def test_a_stream_with_every_backend_down_stops_instead_of_looping(
    two_machines, client, member_key
):
    """ลูปของ stream ต้องเดินไปข้างหน้าเสมอ ไม่งั้นคือ request ที่ไม่มีวันจบ

    เคสนี้พังได้ถ้าลืมย้ายเป้าหมายท้ายลูป — เครื่องเดิมจะถูกยิงซ้ำไม่รู้จบ
    """
    first = respx.post(f"{DIRECT}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("down")
    )
    second = respx.post(f"{SPARE}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("down")
    )

    response = ask(client, member_key, stream=True)

    assert response.status_code == 200          # SSE carries the error in-band
    assert "error" in response.text.lower()
    assert first.call_count == 1 and second.call_count == 1


@respx.mock
def test_a_stream_is_not_replayed_once_the_caller_has_seen_output(
    two_machines, client, member_key
):
    """เนื้อหาที่ส่งออกไปแล้วเรียกคืนไม่ได้ · ยิงใหม่ = ผู้ใช้เห็นคำตอบซ้ำสองรอบ"""
    truncated = httpx.Response(
        200,
        text=f"data: {CHUNK}\n\n",   # ตัดกลางคัน ไม่มี [DONE]
        headers={"content-type": "text/event-stream"},
    )
    respx.post(f"{DIRECT}/v1/chat/completions").mock(return_value=truncated)
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(return_value=sse(CHUNK))

    response = ask(client, member_key, stream=True)

    assert response.status_code == 200
    assert response.text.count("hello") == 1, "ต้องไม่เล่นซ้ำ"
    assert not spare.called, "ส่งออกไปแล้วห้ามสลับเครื่อง"
