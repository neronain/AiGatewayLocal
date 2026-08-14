"""Deleting keys and quota policies, and what must not be deletable.

Both lists could only ever grow. After a term of issuing and rotating, the key
table is mostly tombstones and the live keys are hard to pick out of it; a quota
policy created by mistake could be superseded but never taken away, and since
the most specific match wins, a stale narrow policy quietly keeps beating the
broader one meant to replace it.

The rules worth holding onto are the ones that stop a delete from cutting
somebody off silently.
"""

from __future__ import annotations


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _issue_key(client, user_id, name="temp"):
    response = client.post(
        "/admin/api-keys",
        headers=auth(client.admin_key),
        json={"user_id": user_id, "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _first_user_id(client) -> str:
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    return users[0]["id"]


def _key_ids(client) -> set[str]:
    listing = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()
    return {k["id"] for k in listing["data"]}


# ── keys ────────────────────────────────────────────────────────────────────

def test_a_live_key_cannot_be_deleted(client):
    """Deleting one would cut off whoever holds it, with no row left to name it."""
    user_id = _first_user_id(client)
    key = _issue_key(client, user_id, "still-in-use")

    response = client.delete(f"/admin/api-keys/{key['id']}/purge", headers=auth(client.admin_key))
    assert response.status_code == 400
    assert "Revoke" in response.json()["error"]["message"]
    assert key["id"] in _key_ids(client), "คีย์ต้องยังอยู่"


def test_a_revoked_key_can_be_deleted(client):
    user_id = _first_user_id(client)
    key = _issue_key(client, user_id, "rotated-out")

    client.delete(f"/admin/api-keys/{key['id']}", headers=auth(client.admin_key))  # revoke
    response = client.delete(f"/admin/api-keys/{key['id']}/purge", headers=auth(client.admin_key))
    assert response.status_code == 200
    assert response.json()["purged"] is True
    assert key["id"] not in _key_ids(client)


def test_deleting_a_key_that_is_not_there_says_so(client):
    response = client.delete("/admin/api-keys/nope/purge", headers=auth(client.admin_key))
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"].lower()


def test_the_sweep_takes_revoked_keys_and_leaves_live_ones(client):
    user_id = _first_user_id(client)
    live = _issue_key(client, user_id, "live")
    dead = [_issue_key(client, user_id, f"dead-{i}") for i in range(3)]
    for k in dead:
        client.delete(f"/admin/api-keys/{k['id']}", headers=auth(client.admin_key))

    response = client.post("/admin/api-keys/purge-revoked", headers=auth(client.admin_key))
    assert response.status_code == 200
    assert response.json()["purged"] >= 3

    remaining = _key_ids(client)
    assert live["id"] in remaining
    for k in dead:
        assert k["id"] not in remaining


def test_the_sweep_can_keep_recent_revocations(client):
    """ตอนเพิ่งหมุนคีย์ การเห็นว่าเมื่อกี้ถอนอะไรไปคือประเด็น"""
    user_id = _first_user_id(client)
    key = _issue_key(client, user_id, "revoked-just-now")
    client.delete(f"/admin/api-keys/{key['id']}", headers=auth(client.admin_key))

    response = client.post(
        "/admin/api-keys/purge-revoked?older_than_days=7", headers=auth(client.admin_key)
    )
    assert response.status_code == 200
    assert response.json()["purged"] == 0
    assert key["id"] in _key_ids(client)


def test_a_member_cannot_delete_keys(client, member_key):
    user_id = _first_user_id(client)
    key = _issue_key(client, user_id, "not-yours")
    client.delete(f"/admin/api-keys/{key['id']}", headers=auth(client.admin_key))

    response = client.delete(f"/admin/api-keys/{key['id']}/purge", headers=auth(member_key))
    assert response.status_code in (401, 403)
    assert key["id"] in _key_ids(client)


# ── quota policies ──────────────────────────────────────────────────────────

def _policy_ids(client) -> set[str]:
    data = client.get("/admin/quota-policies", headers=auth(client.admin_key)).json()["data"]
    return {p["id"] for p in data}


def test_a_quota_policy_can_be_deleted(client):
    created = client.post(
        "/admin/quota-policies",
        headers=auth(client.admin_key),
        json={"scope": "global", "window": "day", "max_requests": 10},
    )
    assert created.status_code == 201
    policy_id = created.json()["id"]
    assert policy_id in _policy_ids(client)

    response = client.delete(f"/admin/quota-policies/{policy_id}", headers=auth(client.admin_key))
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert policy_id not in _policy_ids(client)


def test_deleting_a_policy_that_is_not_there_says_so(client):
    response = client.delete("/admin/quota-policies/nope", headers=auth(client.admin_key))
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"].lower()


def test_a_member_cannot_delete_a_quota_policy(client, member_key):
    created = client.post(
        "/admin/quota-policies",
        headers=auth(client.admin_key),
        json={"scope": "global", "window": "day", "max_requests": 5},
    )
    policy_id = created.json()["id"]
    response = client.delete(f"/admin/quota-policies/{policy_id}", headers=auth(member_key))
    assert response.status_code in (401, 403)
    assert policy_id in _policy_ids(client)
