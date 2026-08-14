"""Giving something more time, instead of issuing it again.

An expired key stops working and is kept, which is right. But the only answer
to "the work is not finished, can we have another week" was to issue a new key
and have everybody paste it into their configuration again. The credential was
never the problem; the date was.

Days count from now rather than from the old expiry, so extending something
that lapsed last month gives a full period rather than a date already gone.
"""

from __future__ import annotations

from datetime import UTC, datetime

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


def extend(client, key_id, days, key=None):
    return client.patch(f"/admin/api-keys/{key_id}",
                        headers=auth(key or client.admin_key), json={"days": days})


def days_left(iso: str) -> int:
    return (datetime.fromisoformat(iso) - datetime.now(UTC)).days


def test_a_key_can_be_given_more_time(client):
    key = issue(client, user(client), expires_in_days=3)
    response = extend(client, key["id"], 30)

    assert response.status_code == 200
    assert 28 <= days_left(response.json()["expires_at"]) <= 30


def test_days_count_from_today_not_from_the_old_date(client):
    """ต่อจากวันหมดอายุเดิมที่ผ่านไปแล้ว = ได้วันที่ผ่านไปแล้วอีกที"""
    from app.db.models import ApiKey

    key = issue(client, user(client), expires_in_days=1)

    import asyncio
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.session import session_scope

    async def lapse():
        async with session_scope() as session:
            row = (await session.execute(
                select(ApiKey).where(ApiKey.id == key["id"])
            )).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(days=60)
            await session.commit()

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(lapse())

    response = extend(client, key["id"], 7)
    assert 5 <= days_left(response.json()["expires_at"]) <= 7


def test_an_expired_key_works_again_once_extended(client):
    """นี่คือทั้งหมดที่ต้องการ — ไม่ต้องออกใบใหม่แล้วให้ทุกคนไปแก้ config"""
    from app.db.models import ApiKey

    person = user(client)
    key = issue(client, person, expires_in_days=1)
    plaintext = key["api_key"]

    import asyncio
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.session import session_scope

    async def lapse():
        async with session_scope() as session:
            row = (await session.execute(
                select(ApiKey).where(ApiKey.id == key["id"])
            )).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(days=1)
            await session.commit()

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(lapse())
    assert client.get("/v1/models", headers=auth(plaintext)).status_code == 401

    extend(client, key["id"], 7)
    assert client.get("/v1/models", headers=auth(plaintext)).status_code == 200


def test_the_expiry_can_be_removed_entirely(client):
    key = issue(client, user(client), expires_in_days=3)
    assert extend(client, key["id"], None).json()["expires_at"] is None


def test_a_revoked_key_cannot_be_brought_back(client):
    """เพิกถอนคือการตัดสินใจที่ตั้งใจให้ย้อนกลับไม่ได้ · ต่ออายุต้องไม่กลายเป็นทางลัด"""
    key = issue(client, user(client))
    client.delete(f"/admin/api-keys/{key['id']}", headers=auth(client.admin_key))

    response = extend(client, key["id"], 7)
    assert response.status_code == 400
    assert "revoked" in response.json()["error"]["message"].lower()


def test_zero_or_negative_days_is_refused(client):
    key = issue(client, user(client))
    assert extend(client, key["id"], 0).status_code == 400
    assert extend(client, key["id"], -5).status_code == 400


def test_a_manager_cannot_extend_a_key_outside_their_class(client):
    lecturer = user(client, "lecturer")
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "CS101", "name": "CS101"}).json()
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": lecturer["id"]})
    their_key = issue(client, lecturer)["api_key"]

    outsider = issue(client, user(client, "not-mine"), expires_in_days=3)
    response = extend(client, outsider["id"], 30, key=their_key)
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"].lower()


# ── the same for a temporary allowance ──────────────────────────────────────

def test_a_quota_policy_can_be_extended(client):
    created = client.post("/admin/quota-policies", headers=auth(client.admin_key),
                          json={"scope": "global", "window": "day",
                                "max_requests": 100, "expires_in_days": 3}).json()

    response = client.patch(f"/admin/quota-policies/{created['id']}",
                            headers=auth(client.admin_key), json={"days": 30})
    assert response.status_code == 200
    assert 28 <= days_left(response.json()["expires_at"]) <= 30


def test_a_policy_expiry_can_be_removed(client):
    created = client.post("/admin/quota-policies", headers=auth(client.admin_key),
                          json={"scope": "global", "window": "day",
                                "max_requests": 100, "expires_in_days": 3}).json()
    response = client.patch(f"/admin/quota-policies/{created['id']}",
                            headers=auth(client.admin_key), json={"days": None})
    assert response.json()["expires_at"] is None
