"""ตัวนับและเพดานของโควตาต้องเป็น 64 บิต — และตัวนับที่บวกไม่ได้ต้องไม่ลากตัวอื่นไปด้วย

บน PostgreSQL คอลัมน์ `Integer` คือ int4 · ตัวนับ token ของหน้าต่าง month/term เป็นยอด
สะสม และ `UPDATE … SET x = x + :n` ที่พายอดข้าม 2,147,483,647 *โยน* ("integer out of
range") ไม่ได้วนกลับ · `QuotaService.record` กลืน exception นั้นไว้ทั้งชุด การบวกตัวนับ
นาทีกับตัวนับของ key ที่อยู่ถัดไปจึงถูกข้าม — ตัวนับและลิมิตต่อนาทีของคนคนนั้นค้างไป
จนจบหน้าต่าง · และเพดานที่ตั้งเกิน 2.1 พันล้านทำให้การสร้างนโยบายตอบ 500

SQLite เก็บ INTEGER เป็น 64 บิตอยู่แล้ว ชุดเทสบน SQLite จึงไม่เคยเห็นอาการนี้ —
ส่วนที่ตรวจได้โดยไม่มีเซิร์ฟเวอร์คือ DDL ที่ compile ออกมาและแผนอัปเกรดสคีมา
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from sqlalchemy import BIGINT, INTEGER, VARCHAR, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.core.quota import Charge, Consumption, DatabaseCounterStore, QuotaService
from app.db.dialect import is_postgresql
from app.db.models import Base, QuotaCounter, QuotaPolicy
from app.registry.schema import QuotaDefaults

COUNTER_COLUMNS = ["requests", "text_input_tokens", "visual_input_tokens",
                   "output_tokens", "images"]
POLICY_COLUMNS = ["max_requests", "max_input_tokens", "max_output_tokens", "max_images",
                  "max_requests_per_minute", "max_tokens_per_minute"]
INT4_MAX = 2_147_483_647


# ---------------------------------------------------------------------------
# สคีมาที่สร้างใหม่
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model, columns", [
    (QuotaCounter, COUNTER_COLUMNS), (QuotaPolicy, POLICY_COLUMNS),
], ids=["quota_counters", "quota_policies"])
def test_a_new_postgres_database_gets_64_bit_columns(model, columns):
    ddl = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    lines = {line.strip().split()[0].strip('"'): line for line in ddl.splitlines()
             if line.strip()}
    for column in columns:
        assert " BIGINT" in lines[column], lines[column]


# ---------------------------------------------------------------------------
# ฐานข้อมูลที่มีอยู่แล้ว: แผนอัปเกรด (ไม่ต้องมีเซิร์ฟเวอร์)
# ---------------------------------------------------------------------------
class _Reflected:
    """connection ปลอมของฐานข้อมูลที่มีทุกตาราง โดยคอลัมน์จำนวนเต็มมีชนิดตามที่กำหนด"""

    def __init__(self, dialect, integer_type, overrides: dict | None = None) -> None:
        self.dialect = dialect
        self._integer_type = integer_type
        self._overrides = overrides or {}

    def _inspector(self):
        integer_type, overrides = self._integer_type, self._overrides

        class _I:
            @staticmethod
            def get_table_names():
                return [t.name for t in Base.metadata.sorted_tables]

            @staticmethod
            def get_columns(name):
                found = []
                for column in Base.metadata.tables[name].columns:
                    if (name, column.name) in overrides:
                        kind = overrides[(name, column.name)]
                    elif column.type.python_type is int:
                        kind = integer_type()
                    else:
                        kind = VARCHAR()
                    found.append({"name": column.name, "type": kind})
                return found

        return _I()


@pytest.fixture
def plan(monkeypatch):
    import app.db.session as session_mod

    real = session_mod.inspect
    monkeypatch.setattr(
        session_mod, "inspect",
        lambda conn: conn._inspector() if isinstance(conn, _Reflected) else real(conn),
    )
    return session_mod.plan_widened_columns


def test_an_existing_postgres_database_is_widened_one_statement_per_table(plan):
    """ALTER … TYPE BIGINT เขียนตารางใหม่ทั้งตารางใต้ ACCESS EXCLUSIVE lock —
    ทุกคอลัมน์ของตารางเดียวกันจึงต้องอยู่ในคำสั่งเดียว ไม่ใช่เขียนใหม่ห้ารอบ"""
    statements = plan(_Reflected(postgresql.dialect(), INTEGER))

    by_table = {s.split()[2]: s for s in statements}
    assert set(by_table) == {"quota_counters", "quota_policies"}, statements
    assert len(statements) == 2

    for column in COUNTER_COLUMNS:
        assert f"ALTER COLUMN {column} TYPE BIGINT" in by_table["quota_counters"]
    for column in POLICY_COLUMNS:
        assert f"ALTER COLUMN {column} TYPE BIGINT" in by_table["quota_policies"]
    for statement in statements:
        assert statement.count("ALTER TABLE") == 1
        # ต้อง execute ได้จริงบน Postgres: เป็นข้อความ SQL ที่ parse เป็น text() ได้
        assert str(text(statement)) == statement


def test_the_usage_ledger_is_never_touched(plan):
    """usage_logs คือตารางใหญ่ที่สุดของระบบ และไม่เกี่ยว — token ต่อแถวไม่ใช่ยอดสะสม"""
    statements = plan(_Reflected(postgresql.dialect(), INTEGER))
    assert not [s for s in statements if "usage" in s or "audit" in s]


def test_a_widened_database_plans_nothing(plan):
    """รันซ้ำทุกครั้งที่เริ่มระบบ และทุก worker — ต้องไม่มีอะไรให้ทำเมื่อทำไปแล้ว"""
    assert plan(_Reflected(postgresql.dialect(), BIGINT)) == []


def test_only_the_columns_that_are_still_narrow_are_changed(plan):
    half_done = {("quota_counters", name): BIGINT() for name in COUNTER_COLUMNS[:3]}
    statements = plan(_Reflected(postgresql.dialect(), INTEGER, half_done))

    counters = next(s for s in statements if "quota_counters" in s)
    assert "ALTER COLUMN requests " not in counters
    assert "ALTER COLUMN output_tokens TYPE BIGINT" in counters
    assert "ALTER COLUMN images TYPE BIGINT" in counters


def test_sqlite_needs_no_change_and_gets_none(plan):
    """INTEGER ของ SQLite เก็บได้ 64 บิตไม่ว่าจะประกาศว่าอะไร และเปลี่ยนชนิดในที่ไม่ได้อยู่แล้ว"""
    assert plan(_Reflected(sqlite.dialect(), INTEGER)) == []


# ---------------------------------------------------------------------------
# ฐานข้อมูลที่มีอยู่แล้ว: ของจริง — SQLite เป็นค่าเริ่มต้น · PostgreSQL เมื่อชุดเทสชี้ไปที่มัน
# ---------------------------------------------------------------------------
_OLD_SQLITE = """
CREATE TABLE quota_counters (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    subject_key VARCHAR(160) NOT NULL,
    window_start DATETIME NOT NULL,
    window_end DATETIME NOT NULL,
    requests INTEGER NOT NULL,
    text_input_tokens INTEGER NOT NULL,
    visual_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    images INTEGER NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_counter_window UNIQUE (subject_key, window_start)
)
"""


async def _make_it_a_database_from_before(engine) -> None:
    """ทำให้ตารางโควตาเป็นรูปที่รุ่นก่อนสร้างไว้: คอลัมน์ตัวเลข 32 บิต"""
    if is_postgresql(str(engine.url)):
        async with engine.begin() as conn:
            for table, columns in (("quota_counters", COUNTER_COLUMNS),
                                   ("quota_policies", POLICY_COLUMNS)):
                changes = ", ".join(f"ALTER COLUMN {c} TYPE INTEGER" for c in columns)
                await conn.execute(text(f"ALTER TABLE {table} {changes}"))
    else:
        old = sqlite3.connect(engine.url.database)
        old.execute(_OLD_SQLITE)
        old.commit()
        old.close()


async def test_a_database_from_before_is_upgraded_in_place_and_keeps_its_counts(temp_db):
    """ยอดที่นับไว้ก่อนอัปเกรดต้องอยู่ครบ และตัวนับต้องเดินข้าม 2,147,483,647 ได้"""
    from app.core.quota import window_bounds
    from app.db import session as session_mod

    engine = session_mod.get_engine()
    await _make_it_a_database_from_before(engine)
    if not is_postgresql(str(engine.url)):
        # SQLite: ตารางอื่นยังไม่มี — ให้ create_all สร้างรอบ ๆ ตารางเก่าที่วางไว้
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    start, end = window_bounds("term")
    async with session_mod.session_scope() as session:
        session.add(QuotaCounter(subject_key="user:heavy", window_start=start, window_end=end,
                                 requests=41, text_input_tokens=INT4_MAX - 500))

    await session_mod.init_db()          # ทางเดียวกับที่ทุก worker เดินตอนเริ่มระบบ
    await session_mod.init_db()          # และรันซ้ำได้

    async with engine.begin() as conn:
        assert await conn.run_sync(session_mod.plan_widened_columns) == []
        if is_postgresql(str(engine.url)):
            rows = await conn.execute(text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_name IN ('quota_counters', 'quota_policies') "
                "AND table_schema = current_schema()"
            ))
            kinds = {(t, c): kind for t, c, kind in rows}
            for column in COUNTER_COLUMNS:
                assert kinds[("quota_counters", column)] == "bigint", column
            for column in POLICY_COLUMNS:
                assert kinds[("quota_policies", column)] == "bigint", column

    store = DatabaseCounterStore(session_mod.get_sessionmaker())
    before = await store.get("user:heavy", "term")
    assert (before.requests, before.text_input_tokens) == (41, INT4_MAX - 500)

    await store.increment("user:heavy", "term", Consumption(requests=1, text_input_tokens=1000))
    await store.increment("user:heavy", "term",
                          Consumption(requests=1, text_input_tokens=3_000_000_000))

    after = await store.get("user:heavy", "term")
    assert after.requests == 43
    assert after.text_input_tokens == INT4_MAX + 500 + 3_000_000_000

    # และเพดานที่เกิน 2.1 พันล้านเก็บได้
    async with session_mod.session_scope() as session:
        session.add(QuotaPolicy(scope="global", window="term", max_input_tokens=50_000_000_000))
    async with session_mod.session_scope() as session:
        stored = (await session.execute(
            text("SELECT max_input_tokens FROM quota_policies")
        )).scalar()
    assert stored == 50_000_000_000


# ---------------------------------------------------------------------------
# ตัวนับตัวหนึ่งบวกไม่ได้ ต้องไม่ทำให้ตัวที่เหลือไม่ถูกบวก
# ---------------------------------------------------------------------------
class _OneCounterIsBroken:
    """ที่เก็บจริง ที่ตัวนับตัวหนึ่งโยนทุกครั้งที่ถูกบวก — เหมือน int4 ที่เต็มแล้ว"""

    def __init__(self, inner, broken: tuple[str, str]) -> None:
        self._inner = inner
        self._broken = broken

    async def increment(self, key, window, delta):
        if (key, window) == self._broken:
            raise RuntimeError("integer out of range")
        await self._inner.increment(key, window, delta)

    def __getattr__(self, name):
        return getattr(self._inner, name)


SPENT = Consumption(requests=1, text_input_tokens=10, output_tokens=5)


async def _quota(broken: tuple[str, str]):
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = DatabaseCounterStore(get_sessionmaker())
    return QuotaService(_OneCounterIsBroken(store, broken), QuotaDefaults()), store


async def test_one_counter_failing_does_not_skip_the_ones_after_it(temp_db, caplog):
    """เคสที่อ่านจากโค้ด: ตัวนับรายเทอมของคนชน int4 → ตัวนับนาทีและตัวนับของ key ถูกข้าม"""
    quota, store = await _quota(broken=("user:u1", "term"))
    charge = Charge(windows=(("user:u1", "term"), ("key:k1", "day"), ("key:k1", "month")),
                    minutes=("user:u1", "key:k1"))

    with caplog.at_level(logging.ERROR, logger="app.core.quota"):
        await quota.record("u1", "term", SPENT, charge=charge)     # ต้องไม่โยน

    assert (await store.get("key:k1", "day")).requests == 1
    assert (await store.get("key:k1", "month")).requests == 1
    assert (await store.get("user:u1", "minute")).output_tokens == 5
    assert (await store.get("key:k1", "minute")).output_tokens == 5

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "user:u1" in errors[0].getMessage() and "term" in errors[0].getMessage()
    assert errors[0].exc_info, "ต้องมี traceback ของสาเหตุจริงติดไปด้วย"


async def test_the_same_holds_for_a_caller_that_passes_no_charge(temp_db, caplog):
    """ทางเดินเดิม (ไม่มี Charge) ต้องได้การรับประกันเดียวกัน"""
    quota, store = await _quota(broken=("user:u1", "term"))

    with caplog.at_level(logging.ERROR, logger="app.core.quota"):
        await quota.record("u1", "term", SPENT, rate_limited=True,
                           api_key_id="k1", key_window="day", key_rate_limited=True)

    assert (await store.get("user:u1", "minute")).requests == 1
    assert (await store.get("key:k1", "day")).requests == 1
    assert (await store.get("key:k1", "minute")).requests == 1
    assert ["user:u1" in r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR] == [True]


async def test_a_healthy_record_logs_nothing(temp_db, caplog):
    quota, _store = await _quota(broken=("nobody", "day"))
    with caplog.at_level(logging.ERROR, logger="app.core.quota"):
        await quota.record("u1", "day", SPENT, charge=Charge(windows=(("user:u1", "day"),)))
    assert not caplog.records
