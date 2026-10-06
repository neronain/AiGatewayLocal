"""`/v1/messages/count_tokens` ผ่านด่านสิทธิ์โมเดลเหมือน surface อื่น

เดิมไม่มี: endpoint นี้ resolve alias แล้วตอบเลย · key ที่จำกัดไว้ที่ `[coding]` ถาม
count_tokens กับ `muse-local` ได้ 200 — ทั้งที่ /v1/messages กับโมเดลเดียวกันตอบ 403 ·
ผลคือทางบอกว่า alias ไหนมีอยู่จริง (และหน้าต่าง/อัตรา tokenizer ของมันอ่านจากตัวเลขได้)
สำหรับ key ที่ไม่ควรรู้

กติกา: คำตอบของ count_tokens ต้องเป็นสถานะเดียวกับที่ /v1/messages จะตอบให้ key ใบเดียวกัน
"""

from __future__ import annotations

import httpx
import pytest
import respx

MUSE_NATIVE = "http://dgx01:8000/v1/messages"
CODING = "http://dgx03:8000/v1/chat/completions"
MSG = [{"role": "user", "content": "hello there"}]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


@pytest.fixture
def backends():
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "model": "up",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}))
        respx.post(MUSE_NATIVE).mock(return_value=httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "up",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 2}}))
        yield


def _narrow_key(client) -> str:
    admin = auth(client.admin_key)
    person = client.post("/admin/users", headers=admin, json={"external_id": "dev2"}).json()
    return client.post("/admin/api-keys", headers=admin, json={
        "user_id": person["id"], "name": "k", "models": ["coding"]}).json()["api_key"]


def _count(client, key, model):
    return client.post("/v1/messages/count_tokens", headers=auth(key),
                       json={"model": model, "messages": MSG})


def _call(client, key, model):
    return client.post("/v1/messages", headers=auth(key),
                       json={"model": model, "max_tokens": 8, "messages": MSG})


@pytest.mark.parametrize("model", ["coding", "muse-local", "no-such-model"])
def test_count_tokens_answers_as_the_messages_endpoint_would(backends, client, model):
    key = _narrow_key(client)

    counted, called = _count(client, key, model), _call(client, key, model)

    assert counted.status_code == called.status_code, (counted.text, called.text)
    if counted.status_code != 200:
        assert counted.json()["error"]["code"] == called.json()["error"]["code"]


def test_a_model_outside_the_keys_scope_is_refused(backends, client):
    refused = _count(client, _narrow_key(client), "muse-local")

    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["type"] == "error"
    assert body["error"]["code"] == "MODEL_NOT_PERMITTED"
    assert "input_tokens" not in body


def test_a_permitted_model_is_still_counted(backends, client, member_key):
    for key, model in ((_narrow_key(client), "coding"), (member_key, "muse-local")):
        counted = _count(client, key, model)
        assert counted.status_code == 200, counted.text
        assert counted.json()["input_tokens"] > 0
