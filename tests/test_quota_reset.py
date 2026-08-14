"""Giving somebody their allowance back before the window turns over.

Someone burns a term's quota on one runaway loop on a Tuesday. The only
remedies were to raise their limit permanently or tell them to wait — and both
are the wrong shape, because the limit was right and the person is blocked now.

The counter is cleared. The usage ledger is not: the reports still show what was
spent, and the reset itself is written to the audit log. A reset that erased its
own evidence would be a quiet way to hand out unlimited access.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.quota import (
    Consumption,
    DatabaseCounterStore,
    ResilientCounterStore,
)


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


def spend(client, plaintext, times=1):
    for _ in range(times):
        client.post("/v1/chat/completions", headers=auth(plaintext),
                    json={"model": "coding", "max_tokens": 8,
                          "messages": [{"role": "user", "content": "hi"}]})


def used(client, user_id):
    rows = client.get("/admin/usage/quota", headers=auth(client.admin_key)).json()["data"]
    return next(r for r in rows if r["user_id"] == user_id)


def test_a_spent_allowance_can_be_handed_back(client):
    person = user(client)
    key = issue(client, person)
    spend(client, key["api_key"], times=3)
    assert used(client, person["id"])["used"]["requests"] == 3

    response = client.post(f"/admin/users/{person['id']}/quota/reset",
                           headers=auth(client.admin_key))

    assert response.status_code == 200
    assert response.json()["cleared"]["requests"] == 3
    assert used(client, person["id"])["used"]["requests"] == 0


def test_the_person_can_work_again_immediately(client):
    """ที่ต้องการจริง ๆ — ไม่ใช่ตัวเลขสวย แต่คนคนนั้นยิงคำขอได้อีกครั้ง"""
    person = user(client)
    client.post("/admin/quota-policies", headers=auth(client.admin_key),
                json={"scope": "user", "subject_id": person["id"],
                      "window": "day", "max_requests": 2})
    plaintext = issue(client, person)["api_key"]
    spend(client, plaintext, times=2)

    blocked = client.post("/v1/chat/completions", headers=auth(plaintext),
                          json={"model": "coding", "max_tokens": 8,
                                "messages": [{"role": "user", "content": "hi"}]})
    assert blocked.status_code == 429

    client.post(f"/admin/users/{person['id']}/quota/reset", headers=auth(client.admin_key))

    after = client.post("/v1/chat/completions", headers=auth(plaintext),
                        json={"model": "coding", "max_tokens": 8,
                              "messages": [{"role": "user", "content": "hi"}]})
    # ไม่ใช่ 200 เพราะไม่มี backend จริงในเทส · ที่ต้องพิสูจน์คือ "ไม่ถูกกั้นด้วยโควตาแล้ว"
    # ซึ่งคือการผ่านด่าน 429 ไปถึงชั้น routing
    assert after.status_code != 429


def test_the_usage_records_survive_the_reset(client):
    """ล้างโควตา ไม่ใช่ล้างหลักฐาน — รายงานต้องยังบอกได้ว่าใช้ไปเท่าไร"""
    person = user(client)
    spend(client, issue(client, person)["api_key"], times=2)

    before = client.get("/admin/usage/by-key", headers=auth(client.admin_key)).json()["data"]
    client.post(f"/admin/users/{person['id']}/quota/reset", headers=auth(client.admin_key))
    after = client.get("/admin/usage/by-key", headers=auth(client.admin_key)).json()["data"]

    assert after == before


def test_only_an_admin_can_hand_an_allowance_back(client):
    lecturer = user(client, "lecturer")
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    their_key = issue(client, lecturer)["api_key"]
    student = user(client, "student")

    response = client.post(f"/admin/users/{student['id']}/quota/reset",
                           headers=auth(their_key))

    assert response.status_code in (401, 403)


def test_resetting_an_unknown_person_is_refused(client):
    response = client.post("/admin/users/nobody/quota/reset", headers=auth(client.admin_key))
    assert response.status_code == 400


def test_the_reset_is_written_to_the_audit_log(client):
    person = user(client)
    spend(client, issue(client, person)["api_key"], times=2)
    client.post(f"/admin/users/{person['id']}/quota/reset", headers=auth(client.admin_key))

    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope

    async def payloads():
        async with session_scope() as session:
            rows = (await session.execute(
                select(AuditLog).where(AuditLog.action == "quota.reset")
            )).scalars().all()
            return [r.payload for r in rows]

    entries = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(payloads())

    assert entries, "การคืนโควตาต้องตามรอยได้ว่าใครทำ"
    assert entries[-1]["cleared"]["requests"] == 2


# ── the store beneath it ────────────────────────────────────────────────────
#
# The interesting failure is not in the route. It is that clearing Redis alone
# does not clear anything: the next read misses, decides an earlier outage may
# have left counts in the database, and reseeds from there. The number comes
# straight back and the button looks broken for reasons nobody can see.

class _FakeRedisStore:
    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], Consumption] = {}
        self.fail = False

    async def get(self, key, window):
        if self.fail:
            raise ConnectionError("redis down")
        return self.counts.get((key, window), Consumption())

    async def increment(self, key, window, delta):
        if self.fail:
            raise ConnectionError("redis down")
        current = self.counts.get((key, window), Consumption())
        self.counts[(key, window)] = Consumption(
            requests=current.requests + delta.requests,
            text_input_tokens=current.text_input_tokens + delta.text_input_tokens,
            visual_input_tokens=current.visual_input_tokens + delta.visual_input_tokens,
            output_tokens=current.output_tokens + delta.output_tokens,
            images=current.images + delta.images,
        )

    async def reset(self, key, window):
        if self.fail:
            raise ConnectionError("redis down")
        self.counts.pop((key, window), None)


def _resilient(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/q.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        from app.db.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(prepare())
    redis = _FakeRedisStore()
    return ResilientCounterStore(redis, DatabaseCounterStore(factory)), redis


def test_a_reset_survives_a_redis_restart(tmp_path):
    """เคสที่ทำให้ปุ่มดูเหมือนพัง — ล้าง Redis อย่างเดียวแล้วตัวเลขเดิมกลับมาเอง"""
    store, redis = _resilient(tmp_path)

    async def scenario():
        await store.increment("u1", "day", Consumption(requests=5))
        redis.fail = True                      # Redis ล่ม → นับลงฐานข้อมูลแทน
        await store.increment("u1", "day", Consumption(requests=2))
        redis.fail = False

        await store.reset("u1", "day")

        redis.counts.clear()                   # Redis เด้งกลับมาแบบว่างเปล่า
        return await store.get("u1", "day")

    after = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())

    assert after == Consumption(), "reseed จากฐานข้อมูลต้องไม่พาตัวเลขที่ล้างไปแล้วกลับมา"


def test_an_incomplete_reset_is_reported_rather_than_claimed(tmp_path):
    """Redis ล้างไม่ได้ = ล้างไม่ครบ · บอกไปตรง ๆ ดีกว่าให้ไปเจอเองทีหลัง"""
    from app.core.errors import GatewayError

    store, redis = _resilient(tmp_path)

    async def scenario():
        await store.increment("u1", "day", Consumption(requests=5))
        redis.fail = True
        with pytest.raises(GatewayError):
            await store.reset("u1", "day")

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
