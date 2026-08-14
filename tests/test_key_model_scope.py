"""Changing what a live key may call, without reissuing it.

A key's model list could be set when it was issued and never again. Adding one
model meant revoking a working credential and chasing down everywhere it had
been pasted — so people issued wide keys up front, which is the opposite of what
the scope is for. The narrow key was the one that cost you later.

The scope is a decision that should be as revisable as the expiry already was.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def user(client, external_id="s1"):
    return client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id}).json()


def issue(client, person, **extra):
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": person["id"], "name": "k", **extra}).json()


def amend(client, key_id, key=None, **body):
    return client.patch(f"/admin/api-keys/{key_id}",
                        headers=auth(key or client.admin_key), json=body)


def can_call(client, plaintext, alias):
    """The only question that matters: does the credential in hand reach it."""
    response = client.post("/v1/chat/completions", headers=auth(plaintext),
                           json={"model": alias, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "hi"}]})
    return response.status_code != 403


def test_a_model_can_be_added_to_a_key_already_in_use(client):
    """ที่ต้องการจริง ๆ — key ใบเดิมที่แจกไปแล้ว เรียก model ใหม่ได้"""
    key = issue(client, user(client), models=["coding"])
    plaintext = key["api_key"]
    assert not can_call(client, plaintext, "muse-local")

    response = amend(client, key["id"], models=["coding", "muse-local"])

    assert response.status_code == 200
    assert response.json()["models"] == ["coding", "muse-local"]
    assert can_call(client, plaintext, "muse-local")
    assert can_call(client, plaintext, "coding"), "ของเดิมต้องไม่หาย"


def test_a_model_can_be_taken_away_again(client):
    key = issue(client, user(client), models=["coding", "muse-local"])
    plaintext = key["api_key"]

    amend(client, key["id"], models=["coding"])

    assert can_call(client, plaintext, "coding")
    assert not can_call(client, plaintext, "muse-local")


def test_an_empty_list_lifts_the_restriction(client):
    """[] คือการตัดสินใจว่า 'ไม่จำกัด' ไม่ใช่ช่องว่างที่ยังไม่ได้กรอก"""
    key = issue(client, user(client), models=["coding"])
    plaintext = key["api_key"]

    assert amend(client, key["id"], models=[]).json()["models"] == []
    assert can_call(client, plaintext, "muse-local")


def test_changing_the_models_leaves_the_expiry_alone(client):
    """สองเรื่องนี้อยู่ในคำขอเดียวกันได้ จึงต้องพิสูจน์ว่าไม่เผลอล้างของกันเอง"""
    key = issue(client, user(client), models=["coding"], expires_in_days=30)
    before = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    original = next(k for k in before if k["id"] == key["id"])["expires_at"]

    assert amend(client, key["id"], models=["muse-local"]).json()["expires_at"] == original


def test_changing_the_expiry_leaves_the_models_alone(client):
    key = issue(client, user(client), models=["coding"], expires_in_days=3)

    assert amend(client, key["id"], days=30).json()["models"] == ["coding"]


def test_an_unknown_alias_is_refused_with_the_real_list(client):
    """ปฏิเสธพร้อมบอกว่ามีอะไรให้เลือก ดีกว่าปล่อยผ่านแล้วไปตายตอนเรียกใช้"""
    key = issue(client, user(client), models=["coding"])

    response = amend(client, key["id"], models=["coding", "not-a-model"])

    assert response.status_code == 404
    body = response.json()["error"]
    assert "not-a-model" in body["message"]
    assert "coding" in body["details"]["known_models"]


def test_a_refused_change_leaves_the_key_as_it_was(client):
    key = issue(client, user(client), models=["coding"])
    plaintext = key["api_key"]

    amend(client, key["id"], models=["muse-local", "not-a-model"])

    assert can_call(client, plaintext, "coding")
    assert not can_call(client, plaintext, "muse-local")


def test_a_manager_cannot_widen_a_key_past_their_own_reach(client):
    """แก้ scope คือการให้สิทธิ์ · เกณฑ์ต้องเท่ากับตอนออก key ไม่ใช่หลวมกว่า"""
    lecturer = user(client, "lecturer")
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "CS101", "name": "CS101"}).json()
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": lecturer["id"]})
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": ["coding"]})
    their_key = issue(client, lecturer)["api_key"]

    student = user(client, "student")
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": student["id"]})
    theirs = issue(client, student, models=["coding"])

    response = amend(client, theirs["id"], key=their_key, models=["coding", "muse-local"])

    assert response.status_code in (400, 403)
    assert client.get("/admin/api-keys", headers=auth(client.admin_key)).json()
    assert amend(client, theirs["id"], key=their_key, models=["coding"]).status_code == 200


def test_a_revoked_key_cannot_have_its_scope_changed(client):
    """เพิกถอนแล้วคือจบ — ไม่ใช่ทางลัดกลับมาใช้ใหม่"""
    key = issue(client, user(client), models=["coding"])
    client.delete(f"/admin/api-keys/{key['id']}", headers=auth(client.admin_key))

    response = amend(client, key["id"], models=["coding", "muse-local"])

    assert response.status_code == 400
    assert "revoked" in response.json()["error"]["message"].lower()


def test_a_request_that_changes_nothing_says_what_it_accepts(client):
    key = issue(client, user(client))

    response = client.patch(f"/admin/api-keys/{key['id']}",
                            headers=auth(client.admin_key), json={"nothing": 1})

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "days" in message and "models" in message


def test_models_must_be_a_list_of_names(client):
    key = issue(client, user(client))

    assert amend(client, key["id"], models="coding").status_code == 400
    assert amend(client, key["id"], models=[1, 2]).status_code == 400


def test_the_change_is_written_to_the_audit_log(client):
    """เปลี่ยนสิทธิ์ของ credential ที่ใช้งานอยู่ ต้องตามรอยได้ว่าใครเปลี่ยนจากอะไรเป็นอะไร"""
    key = issue(client, user(client), models=["coding"])
    amend(client, key["id"], models=["muse-local"])

    import asyncio

    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope

    async def entry():
        async with session_scope() as session:
            rows = (await session.execute(
                select(AuditLog).where(AuditLog.action == "apikey.amend")
            )).scalars().all()
            return [r.payload for r in rows]

    payloads = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(entry())

    assert payloads, "การเปลี่ยน scope ต้องถูกบันทึก"
    assert payloads[-1]["models"] == {"from": ["coding"], "to": ["muse-local"]}
