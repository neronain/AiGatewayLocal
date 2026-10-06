"""backend ตอบ 200 แต่เนื้อในคือความล้มเหลว — ต้องไม่กลายเป็นคำตอบว่างที่สำเร็จ

ตรวจพบ 2026-10-06 · vLLM รายงานความล้มเหลวที่เกิดหลัง stream เปิดแล้วด้วย
`data: {"error": {...}}` เพราะสถานะ 200 ถูกส่งไปก่อน · /v1/messages กับ /v1/responses ทิ้ง
chunk ที่ไม่มี `choices` ข้อความนั้นจึงหายไปทั้งก้อน:

    messages   message_start → message_delta {"stop_reason":"end_turn"} → message_stop
    responses  response.created → response.completed
    แถว usage  success/200 ทั้งสาม surface (chat ส่ง chunk ต่อ แต่ก็บันทึกว่า success)

คำตอบที่ไม่ได้ stream มีรูปเดียวกันอีกสามแบบ: body เป็น JSON list หรือ `null` → TypeError →
500 · หน้า HTML ของ proxy → 502 แต่ไม่มีแถว usage และเครื่องถูกนับว่าตอบสำเร็จ ·
`{"error": …}` กับสถานะ 200 → ส่งต่อเป็นความสำเร็จ
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.realistic_backends import (
    CODING,
    DONE,
    ROLE,
    SPARE,
    STOP,
    SURFACE_NAMES,
    add_spare,
    auth,
    ended_normally,
    endpoint_health,
    quota_used,
    read_stream,
    request_for,
    sse,
    streaming,
    terminal_error,
    text_of,
    usage_chunk,
    usage_rows,
    words,
)

ENGINE_DIED = {"error": {"message": "EngineDeadError: the engine died unexpectedly",
                         "type": "InternalServerError", "param": None, "code": 500}}
TOO_LONG = {"error": {"message": "This model's maximum context length is 262144 tokens. "
                                 "However, you requested 300000 tokens",
                      "type": "BadRequestError", "param": None, "code": 400}}
GOOD = (ROLE, *words(3), STOP, usage_chunk(7, 3), DONE)


@pytest.fixture
def two_machines(writable_config):
    add_spare(writable_config)
    return writable_config


def _error(response) -> dict:  # noqa: ANN001
    return response.json()["error"]


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_an_error_object_mid_stream_ends_the_turn_as_an_error(client, member_key, surface):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(ROLE, *words(2), sse(ENGINE_DIED), DONE))

    response, events = read_stream(client, member_key, surface)

    assert response.status_code == 200 and "word1" in text_of(surface, events)
    assert not ended_normally(surface, events), "ห้ามปิดเป็นคำตอบที่จบปกติ"
    error = terminal_error(surface, events)
    assert error is not None, [e for e, _ in events]
    assert "engine died" in error["message"], "ข้อความของ backend คือสิ่งเดียวที่บอกว่าเกิดอะไร"
    row = usage_rows(client)[-1]
    assert (row["status"], row["error_code"], row["http_status"]) == (
        "error", "UPSTREAM_ERROR", 502)
    health = endpoint_health(client)
    assert health["total_failures"] == 1, "นับเหมือน HTTP 5xx"
    assert "engine died" in health["last_error"]


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_an_error_object_as_the_first_payload_fails_over(
    two_machines, client, member_key, surface
):
    """ยังไม่มีอะไรถึงผู้เรียก — error ของเครื่องแรกจึงสลับเครื่องได้เหมือน HTTP 5xx"""
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(sse(ENGINE_DIED), DONE))
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(*GOOD))

    response, events = read_stream(client, member_key, surface)

    assert spare.called and response.status_code == 200
    assert ended_normally(surface, events) and "word2" in text_of(surface, events)
    assert response.headers["x-litegate-endpoint"] == "spare"
    assert usage_rows(client)[-1]["status"] == "success"


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_context_overflow_reported_inside_the_stream_is_a_400(client, member_key, surface):
    """คำตัดสินเรื่อง *คำขอ* — ผู้เรียกต้องได้ 400 ที่บอกให้ย่อ และเครื่องไม่ถูกนับว่าล้ม"""
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(sse(TOO_LONG), DONE))

    path, body = request_for(surface, stream=True)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 400, response.text
    error = _error(response)
    assert (error.get("code") or "") == "CONTEXT_LENGTH_EXCEEDED"
    assert "262144" in error["message"]
    assert endpoint_health(client)["total_failures"] == 0, (
        "prompt ยาวเกินของผู้ใช้คนเดียวต้องไม่ทำให้ backend ที่ดีอยู่ถูกตีว่าล้ม"
    )
    assert usage_rows(client)[-1]["error_code"] == "CONTEXT_LENGTH_EXCEEDED"


# ── ไม่ stream ─────────────────────────────────────────────────────────────────
UNUSABLE = {
    "html": b"<html><body>502 Bad Gateway - nginx</body></html>",
    "json-list": b"[1, 2, 3]",
    "json-null": b"null",
    "error-object": b'{"error": {"message": "model is loading", "code": 503}}',
}


@respx.mock
@pytest.mark.parametrize("body_kind", UNUSABLE)
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_200_with_an_unusable_body_is_a_clean_upstream_error(
    client, member_key, surface, body_kind
):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=UNUSABLE[body_kind]))

    path, body = request_for(surface)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 502, response.text
    error = _error(response)
    assert (error.get("code") or "") == "UPSTREAM_ERROR"
    if body_kind == "error-object":
        assert "model is loading" in error["message"]
    rows = usage_rows(client)
    assert len(rows) == 1, "ต้องมีแถว usage"
    assert (rows[0]["status"], rows[0]["http_status"], rows[0]["error_code"]) == (
        "error", 502, "UPSTREAM_ERROR")
    assert endpoint_health(client)["total_failures"] == 1, "เครื่องต้องถูกนับว่าล้ม"
    assert quota_used(client)["output_tokens"] == 0


@respx.mock
@pytest.mark.parametrize("body_kind", UNUSABLE)
def test_a_200_with_an_unusable_body_is_retried_on_the_other_machine(
    two_machines, client, member_key, body_kind
):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=UNUSABLE[body_kind]))
    reply = {"id": "c", "object": "chat.completion", "model": "up",
             "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "ok"}}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply))

    path, body = request_for("chat")
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 200 and spare.called, response.text
    assert response.headers["x-litegate-endpoint"] == "spare"
    assert response.json()["choices"][0]["message"]["content"] == "ok"


@respx.mock
def test_a_request_fault_inside_a_200_is_not_the_machines_fault(client, member_key):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=TOO_LONG))

    path, body = request_for("chat")
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 400
    assert _error(response)["code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert endpoint_health(client)["total_failures"] == 0
