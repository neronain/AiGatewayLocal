"""An administrator left without a password by an upgrade can still get in.

Console sign-in arrived after this deployment did. The bootstrap that creates the
first administrator returns early when one already exists, so a database from
before the change keeps an admin row with an empty hash — and nobody can reach
the console at all. That is not a policy decision, it is a gap left by an
upgrade, and the system should close it rather than expect somebody to edit the
database by hand.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def chosen_password(monkeypatch):
    from app import config as config_mod

    monkeypatch.setenv("GW_ADMIN_PASSWORD", "chosen-by-the-operator")
    config_mod.get_settings.cache_clear()
    yield "chosen-by-the-operator"
    config_mod.get_settings.cache_clear()


def _strip_password(client):
    """ทำให้เหมือน DB ที่มาจากรุ่นก่อนมีรหัสผ่าน"""
    import asyncio

    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import session_scope

    async def wipe():
        async with session_scope() as session:
            row = (await session.execute(select(User).where(User.role == "admin"))).scalars().first()
            row.password_hash = ""
            await session.commit()
            return row.external_id

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(wipe())


def test_an_admin_without_a_password_is_given_one(client, chosen_password):
    import asyncio

    from app.main import _bootstrap_admin

    who = _strip_password(client)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_bootstrap_admin(client.app))

    response = client.post("/auth/login", json={"username": who, "password": chosen_password})
    assert response.status_code == 200


def test_an_admin_that_already_has_one_is_left_alone(client, chosen_password):
    """ซ่อมเฉพาะสภาพที่พัง · เขียนทับรหัสผ่านที่ใช้อยู่คือการล็อกเจ้าของออกจากระบบ"""
    import asyncio

    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import session_scope
    from app.main import _bootstrap_admin

    async def hash_now():
        async with session_scope() as session:
            row = (await session.execute(select(User).where(User.role == "admin"))).scalars().first()
            return row.password_hash

    loop = asyncio.get_event_loop_policy().new_event_loop()
    before = loop.run_until_complete(hash_now())
    assert before, "เทสนี้ต้องเริ่มจาก admin ที่มีรหัสผ่านอยู่แล้ว"
    loop.run_until_complete(_bootstrap_admin(client.app))
    assert loop.run_until_complete(hash_now()) == before


def test_only_one_worker_announces_the_password(client, chosen_password):
    """uvicorn รันหลาย worker และทุกตัวรัน startup hook นี้

    ต่างคนต่างสุ่มแล้วเขียนทับกัน = log มีรหัสผ่านหลายอันแต่ใช้ได้อันเดียว แล้วคนอ่าน
    log ก็หยิบอันแรกไปลอง · เจอจริงตอน deploy: พิมพ์ออกมาสี่อัน ใช้ได้อันสุดท้าย
    """
    import asyncio
    import logging

    from app.main import _bootstrap_admin

    who = _strip_password(client)
    loop = asyncio.get_event_loop_policy().new_event_loop()

    announced = []
    handler = logging.Handler()
    handler.emit = lambda record: announced.append(record.getMessage())
    log = logging.getLogger("app.main")
    log.addHandler(handler)
    try:
        for _ in range(4):                       # เหมือนสี่ worker เรียกพร้อมกัน
            loop.run_until_complete(_bootstrap_admin(client.app))
    finally:
        log.removeHandler(handler)

    restored = [m for m in announced if "RESTORED" in m]
    assert len(restored) == 1, f"ประกาศ {len(restored)} ครั้ง — ต้องมีผู้ชนะคนเดียว"
    assert client.post("/auth/login",
                       json={"username": who, "password": chosen_password}
                       ).status_code == 200


def test_nothing_happens_while_some_admin_can_still_sign_in(client, chosen_password):
    """deployment จริงมี admin หลายคน · เจอคนหนึ่งไม่มีรหัสผ่านไม่ได้แปลว่าใครเข้าไม่ได้

    ตั้งรหัสให้เขาทั้งที่คนอื่นเข้าได้อยู่แล้ว = แตะระบบที่ไม่ได้พัง แล้วพิมพ์ความลับ
    ออก log โดยไม่มีใครต้องการ · เคสจริง: gateway ที่มี 'admin' (มีรหัส) กับ 'it' (ไม่มี)
    """
    import asyncio

    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import session_scope
    from app.main import _bootstrap_admin

    loop = asyncio.get_event_loop_policy().new_event_loop()

    async def add_second_admin_without_password():
        async with session_scope() as session:
            session.add(User(external_id="it", display_name="IT", role="admin",
                             password_hash=""))
            await session.commit()

    async def hashes():
        async with session_scope() as session:
            rows = (await session.execute(select(User).where(User.role == "admin"))).scalars()
            return {r.external_id: r.password_hash for r in rows}

    loop.run_until_complete(add_second_admin_without_password())
    before = loop.run_until_complete(hashes())
    loop.run_until_complete(_bootstrap_admin(client.app))
    assert loop.run_until_complete(hashes()) == before, "ไม่ควรแตะอะไรเลย"
