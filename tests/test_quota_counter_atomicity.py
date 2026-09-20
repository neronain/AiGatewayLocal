"""ตัวนับโควตาในฐานข้อมูลต้องบวกโดยไม่ทำการบวกของคนอื่นหาย

เดิมเป็น read-modify-write ในภาษา Python (SELECT → `+=` → UPDATE ยอดรวม) ซึ่งพึ่ง
ความจริงที่ว่า SQLite ให้เขียนได้ทีละคน · ความจริงข้อนั้นหายไปทันทีที่ลูกค้าย้ายไป
PostgreSQL ซึ่งรับการเขียนพร้อมกันได้จริง — และการย้ายไป Postgres คือสิ่งที่ลูกค้า
หลาย instance ทำเป็นอย่างแรก

การบวกที่หายไปเข้าข้างผู้ใช้เสมอ (นับได้น้อยกว่าที่ใช้จริง) แปลว่ามันจะไม่มีใคร
ร้องเรียน มันจะโผล่เป็นบิลที่ไม่ตรงกับของที่ backend เผาไปจริง ๆ เท่านั้น
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.core.quota import Consumption, DatabaseCounterStore


async def test_concurrent_increments_all_land(temp_db):
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())

    # 12 ไม่ใช่ 60 โดยตั้งใจ · read-modify-write เสียหายตั้งแต่สองตัวที่สลับกันทำงาน
    # การยิง 60 writer ใส่ SQLite เพิ่มแค่ "และ SQLite ต้องรอดจากการแย่งเขียน" ซึ่งไม่ใช่
    # คุณสมบัติที่เทสนี้ทดสอบ และขึ้นกับความเร็วดิสก์ของเครื่องที่รัน — แดงบน CI runner
    # (database is locked) ทั้งที่เครื่องพัฒนาผ่านตลอด
    #
    # ความเป็น atomic ตัวจริงถูกพิสูจน์แบบไม่ต้องแย่งกันที่
    # test_the_increment_is_a_self_referencing_update_on_both_dialects (ตรวจ SQL ที่ compile
    # ออกมาว่าเป็น SET x = x + n) และการเขียนพร้อมกันจริงถูกพิสูจน์บน Postgres
    # ที่ tests/test_postgres_support.py ซึ่งรับ writer พร้อมกันได้จริง
    rounds = 12
    await asyncio.gather(*[
        store.increment("u:somchai", "day", Consumption(requests=1, output_tokens=7))
        for _ in range(rounds)
    ])

    got = await store.get("u:somchai", "day")
    assert got.requests == rounds, "มีการบวกหายไป — ตัวนับกลับไปเป็น read-modify-write แล้ว"
    assert got.output_tokens == 7 * rounds


async def test_the_first_increment_creates_the_row_with_its_own_delta(temp_db):
    """แถวแรกต้องพกยอดของรอบนั้นไปด้วย ไม่ใช่สร้างเป็นศูนย์แล้วค่อยบวกทีหลัง"""
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())

    await store.increment("u:new", "day", Consumption(requests=3, images=2))
    got = await store.get("u:new", "day")

    assert (got.requests, got.images) == (3, 2)


async def test_reset_clears_only_this_subject(temp_db):
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())

    await store.increment("u:a", "day", Consumption(requests=5))
    await store.increment("u:b", "day", Consumption(requests=9))
    await store.reset("u:a", "day")

    assert (await store.get("u:a", "day")).requests == 0
    assert (await store.get("u:b", "day")).requests == 9


async def test_the_increment_is_a_self_referencing_update_on_both_dialects():
    """โครงสร้างของ SQL ที่โค้ดจริงสร้าง ไม่ใช่แค่ผลลัพธ์

    ผลลัพธ์ของเทสข้างบนผ่านได้ด้วย read-modify-write เหมือนกัน ถ้าจังหวะของ event loop
    บังเอิญไม่ชนกัน · สิ่งที่ต้องคงไว้จริง ๆ คือ "ฐานข้อมูลเป็นคนบวก" ซึ่งอ่านออกจาก
    SQL ที่ถูก compile ได้ตรง ๆ และอ่านออกเหมือนกันทั้งสอง dialect
    """
    captured: list = []

    class _Result:
        rowcount = 1

    class _Session:
        async def execute(self, statement):
            captured.append(statement)
            return _Result()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    await DatabaseCounterStore._add_to_existing(
        _Session(), "u:x", datetime(2026, 3, 1, tzinfo=UTC),
        Consumption(requests=1, output_tokens=2, images=3),
    )
    assert len(captured) == 1, "หนึ่งการบวก = หนึ่งคำสั่ง ไม่ใช่ SELECT แล้วค่อย UPDATE"

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        sql = str(captured[0].compile(dialect=dialect)).replace("\n", " ")
        flat = " ".join(sql.split()).lower()
        assert flat.startswith("update quota_counters set"), (dialect.name, flat)
        for column in ("requests", "output_tokens", "images"):
            # ค่าใหม่ต้องอ้างถึงค่าเดิมของคอลัมน์นั้นเอง = ฐานข้อมูลเป็นคนอ่านและบวก
            assert f"coalesce(quota_counters.{column}" in flat, (dialect.name, column, flat)


async def test_losing_the_insert_race_does_not_lose_the_count(temp_db, monkeypatch):
    """สองตัวเห็นว่า "ยังไม่มีแถว" พร้อมกัน แล้วต่างคนต่างไป INSERT

    คนที่แพ้ชน unique constraint · ถ้าไม่ลองใหม่ ยอดของรอบนั้นหายไปทั้งก้อน
    เป็นสาขาที่ไม่มีทางเกิดจากการเรียกธรรมดา — บังคับให้ UPDATE รอบแรกมองไม่เห็น
    แถวที่มีอยู่จริง เพื่อเดินเข้าไปดูว่ามันฟื้นตัวถูกไหม
    """
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())
    await store.increment("u:x", "day", Consumption(requests=1))

    real = DatabaseCounterStore._add_to_existing
    calls = {"n": 0}

    async def first_update_sees_nothing(session, key, start, delta):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return await real(session, key, start, delta)

    monkeypatch.setattr(
        DatabaseCounterStore, "_add_to_existing", staticmethod(first_update_sees_nothing)
    )
    await store.increment("u:x", "day", Consumption(requests=10))

    assert calls["n"] == 2, "ต้องลอง UPDATE ใหม่หลัง INSERT ชน unique constraint"
    assert (await store.get("u:x", "day")).requests == 11


async def test_a_locked_sqlite_file_is_waited_out_not_given_up_on(temp_db, monkeypatch):
    """ยอมแพ้ตอนไฟล์ถูกล็อก = การบวกหายจริง ซึ่งคือสิ่งเดียวกับที่เมธอดนี้ถูกเขียนมาแก้

    WAL + busy_timeout ทำให้คนเขียนที่มาทีหลัง *รอ* แทนที่จะแพ้ แต่รอจนหมดเวลาก็ยัง
    เป็นไปได้เมื่อคนเขียนเยอะพร้อมกันบนดิสก์ช้า — เจอจริงบน CI runner (60 การบวก
    พร้อมกัน → "database is locked") ทั้งที่บนเครื่องพัฒนาผ่านตลอด
    """
    from sqlalchemy.exc import OperationalError

    from app.core import quota as quota_mod
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())
    real = store._add_to_existing
    calls = {"n": 0}

    async def flaky(session, key, start, delta):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("UPDATE quota_counters", {}, Exception("database is locked"))
        return await real(session, key, start, delta)

    monkeypatch.setattr(store, "_add_to_existing", flaky)
    monkeypatch.setattr(quota_mod, "_LOCK_BACKOFF_S", 0)

    await store.increment("u:locked", "day", Consumption(requests=1, output_tokens=3))
    await store.increment("u:locked", "day", Consumption(requests=1, output_tokens=3))

    got = await store.get("u:locked", "day")
    assert got.requests == 2 and got.output_tokens == 6, "การบวกหายระหว่างรอล็อก"
    assert calls["n"] >= 3, "ต้องลองใหม่จริง ไม่ใช่ผ่านเพราะไม่เคยล็อก"


async def test_an_error_that_is_not_a_lock_is_raised_immediately(temp_db, monkeypatch):
    """ลองใหม่กับทุก OperationalError = กลืนบั๊กจริง (สคีมาผิด/ดิสก์เต็ม) แล้วช้าลงสี่เท่า"""
    from sqlalchemy.exc import OperationalError

    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())
    calls = {"n": 0}

    async def broken(session, key, start, delta):
        calls["n"] += 1
        raise OperationalError("UPDATE quota_counters", {}, Exception("no such column: requests"))

    monkeypatch.setattr(store, "_add_to_existing", broken)
    with pytest.raises(OperationalError):
        await store.increment("u:broken", "day", Consumption(requests=1))
    assert calls["n"] == 1, "ต้องเด้งทันที ไม่ลองใหม่"
