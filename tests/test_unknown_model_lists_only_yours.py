"""ข้อความ "ไม่มีโมเดลนี้" ต้องไม่เปิดแค็ตตาล็อกที่ /v1/models ซ่อนไว้จาก key ใบนั้น

ตรวจ 2026-10-06: key ที่จำกัดไว้ที่ `[coding]` เรียกชื่อที่ไม่มีอยู่ ได้

    Model 'no-such-model' does not exist. Available models: coding, gemma-vision, muse-local.

ทั้งที่ `/v1/models` ของ key ใบเดียวกันตอบแค่ `coding` · รายชื่อในข้อความกรองตาม role
อย่างเดียว ไม่ได้ผ่านสิทธิ์ของ key/workspace — พิมพ์ชื่อผิดครั้งเดียวก็รู้ว่าเกตเวย์มีอะไรบ้าง

กติกาที่เทสนี้ยึด: **รายชื่อในข้อความ ⊆ รายชื่อใน /v1/models ของผู้เรียกคนเดียวกัน** บนทุก
surface ที่มีข้อความนี้ — เทียบกับ /v1/models จริง ไม่ใช่กับลิสต์ที่เขียนมือไว้ในเทส
"""

from __future__ import annotations

import pytest

# (path, body ที่เหลือนอกจาก model) — ทุกทางที่ resolve alias
SURFACES = [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages", {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages/count_tokens", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"input": "hi"}),
    ("/v1/embeddings", {"input": "hi"}),
    ("/v1/rerank", {"query": "hi", "documents": ["a"]}),
]
IDS = ["chat", "messages", "count_tokens", "responses", "embeddings", "rerank"]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def _narrow_key(client, models: list[str]) -> str:
    admin = auth(client.admin_key)
    person = client.post("/admin/users", headers=admin, json={"external_id": "dev2"}).json()
    return client.post("/admin/api-keys", headers=admin,
                       json={"user_id": person["id"], "name": "k", "models": models},
                       ).json()["api_key"]


def _listed(client, key: str) -> set[str]:
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def _error(response) -> dict:
    return response.json()["error"]


@pytest.mark.parametrize("path, body", SURFACES, ids=IDS)
def test_a_scoped_key_is_told_only_about_its_own_models(client, path, body):
    key = _narrow_key(client, ["coding"])
    listed = _listed(client, key)
    assert listed == {"coding"}, "ตัวควบคุม: key ใบนี้ต้องเห็นแค่ coding"

    response = client.post(path, headers=auth(key), json={"model": "no-such-model", **body})

    assert response.status_code == 404, response.text
    error = _error(response)
    assert error["code"] == "MODEL_NOT_FOUND"
    for hidden in ("gemma-vision", "muse-local"):
        assert hidden not in error["message"], error["message"]
    assert "coding" in error["message"], "ยังต้องช่วยคนพิมพ์ผิด ด้วยชื่อที่เขาใช้ได้"
    if "details" in error:      # /v1/messages ใช้ซองของ Anthropic ซึ่งไม่มี details
        assert set(error["details"]["available_models"]) <= listed


@pytest.mark.parametrize("path, body", SURFACES, ids=IDS)
def test_an_unscoped_member_still_gets_the_whole_list(client, member_key, path, body):
    """ไม่ได้ตัดความช่วยเหลือทิ้ง: คนที่เห็นทุกตัวอยู่แล้วยังได้รายชื่อครบเท่า /v1/models"""
    listed = _listed(client, member_key)
    assert {"coding", "gemma-vision", "muse-local"} <= listed

    response = client.post(path, headers=auth(member_key),
                           json={"model": "no-such-model", **body})

    assert response.status_code == 404, response.text
    message = _error(response)["message"]
    for alias in listed:
        assert alias in message


def test_someone_who_may_call_nothing_is_told_so_not_shown_the_catalogue(client):
    """อยู่ในกลุ่มที่ยังไม่เปิดโมเดลสักตัว — ไม่มีอะไรให้ใช้ ก็ต้องไม่มีอะไรให้ดู"""
    admin = auth(client.admin_key)
    person = client.post("/admin/users", headers=admin, json={"external_id": "s7"}).json()
    empty = client.post("/admin/workspaces", headers=admin,
                        json={"code": "EMPTY", "name": "EMPTY"}).json()
    client.post(f"/admin/workspaces/{empty['id']}/join", headers=admin,
                json={"user_id": person["id"]})
    key = client.post("/admin/api-keys", headers=admin,
                      json={"user_id": person["id"], "name": "k"}).json()["api_key"]
    assert _listed(client, key) == set()

    response = client.post("/v1/chat/completions", headers=auth(key), json={
        "model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 404
    error = _error(response)
    assert error["details"]["available_models"] == []
    for hidden in ("coding", "gemma-vision", "muse-local"):
        assert hidden not in error["message"]
