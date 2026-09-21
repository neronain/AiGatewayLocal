"""PostgreSQL ต้องใช้ได้จริง โดยที่ SQLite ยังเป็นค่าเริ่มต้นและไม่เปลี่ยนพฤติกรรม

เทสส่วนใหญ่ในไฟล์นี้ **ไม่ต้องมี Postgres จริง** — มันตรวจที่ระดับ SQL ที่ถูก compile
ออกมา ซึ่งจับบั๊กที่เจอจริงได้ทั้งสองตัว (DEFAULT ของ boolean และคำสงวน `window`)
โดยไม่บังคับให้ทุกคนที่รันเทสต้องลงเซิร์ฟเวอร์ฐานข้อมูล

ใครมี Postgres และอยากรันของจริงด้วย ตั้ง GW_TEST_POSTGRES_URL แล้วเทสท้ายไฟล์จะทำงาน
ไม่ตั้ง = ข้ามแบบบอกเหตุผล ไม่ใช่ล้ม

    GW_TEST_POSTGRES_URL=postgresql+asyncpg://litegate:pw@localhost/litegate_test \\
      .venv/bin/python -m pytest tests/test_postgres_support.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.db.dialect import dialect_name, is_postgresql, is_sqlite, utc_date
from app.db.models import Base, QuotaPolicy, UsageLog

PG_URL = os.environ.get("GW_TEST_POSTGRES_URL", "")

PG = postgresql.dialect()
LITE = sqlite.dialect()


# ── ค่าเริ่มต้นห้ามขยับ ────────────────────────────────────────────────────────

def test_sqlite_is_still_the_default():
    """ลูกค้าเครื่องเดียวต้องลงแล้วรันได้ โดยไม่ต้องมีเซิร์ฟเวอร์ฐานข้อมูล"""
    from app.config import Settings

    assert Settings().database_url.startswith("sqlite+aiosqlite")


def test_asyncpg_is_optional_not_a_core_dependency():
    """asyncpg อยู่ใน extra ชื่อ postgres — ไม่ใช่ของที่ทุก deployment ต้องลง"""
    from pathlib import Path

    try:
        import tomllib
    except ModuleNotFoundError:            # Python 3.10 — tomllib เข้ามาใน 3.11
        import tomli as tomllib

    data = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
    )
    core = " ".join(data["project"]["dependencies"])
    extras = data["project"]["optional-dependencies"]

    assert "asyncpg" not in core, "asyncpg ต้องไม่อยู่ใน dependencies หลัก"
    assert "aiosqlite" in core, "SQLite ยังต้องเป็นของหลัก"
    assert any("asyncpg" in item for item in extras["postgres"])
    assert any("orjson" in item for item in extras["speed"])


@pytest.mark.parametrize("url,name,sqlite_flag,pg_flag", [
    ("sqlite+aiosqlite:///./data/gateway.db", "sqlite", True, False),
    ("sqlite:///:memory:", "sqlite", True, False),
    ("postgresql+asyncpg://u:p@h:5432/db", "postgresql", False, True),
    ("postgresql://u:p@h/db", "postgresql", False, True),
    ("postgres://u:p@h/db", "postgres", False, True),
])
def test_dialect_is_read_from_the_url(url, name, sqlite_flag, pg_flag):
    assert dialect_name(url) == name
    assert is_sqlite(url) is sqlite_flag
    assert is_postgresql(url) is pg_flag


# ── จุดที่ภาษา SQL ต่างกันจริง ─────────────────────────────────────────────────

def test_daily_buckets_are_cut_on_utc_on_both_dialects():
    """`func.date(ts)` ใช้ได้ทั้งคู่แต่ได้คนละคำตอบ — ตัวที่เราใช้ต้องบังคับ timezone.utc

    บน Postgres คอลัมน์เป็น timestamptz และ `date()` แปลงตามค่า TimeZone ของ session
    ก่อน · ถ้าไม่บังคับ รายงานรายวันของลูกค้าที่เซิร์ฟเวอร์ไม่ได้ตั้งเป็น timezone.utc จะเลื่อน
    ขอบวันไปเงียบ ๆ และไม่ตรงกับ `WHERE ts >= since` ที่คิดแบบ timezone.utc อยู่แล้ว
    """
    rendered_pg = str(select(utc_date(UsageLog.ts)).compile(dialect=PG))
    rendered_lite = str(select(utc_date(UsageLog.ts)).compile(dialect=LITE))

    assert "AT TIME ZONE 'UTC'" in rendered_pg
    assert "::date" in rendered_pg
    # SQLite เก็บเป็นเวลา timezone.utc แบบไม่มี tz อยู่แล้ว — date() ตัดถูกตั้งแต่แรก
    assert "date(usage_logs.ts)" in rendered_lite
    assert "AT TIME ZONE" not in rendered_lite


def test_boolean_defaults_are_rendered_in_the_dialects_own_spelling():
    """บั๊กจริง: `ALTER TABLE ... ADD COLUMN x BOOLEAN DEFAULT 0` ล้มบน Postgres

    SQLite ไม่มีชนิด boolean จริงจึงรับ 0/1 ได้ · Postgres ตอบ `column ... is of
    type boolean but default expression is of type integer` แล้วการอัปเกรดสคีมา
    ตอนสตาร์ตล้มทั้งก้อน — เกตเวย์ขึ้นไม่ได้เลย ไม่ใช่แค่คอลัมน์นั้นหาย
    """
    from app.db.session import _default_literal

    booleans = [
        (t, c) for t in Base.metadata.sorted_tables for c in t.columns
        if type(c.type).__name__ == "Boolean" and _default_literal(c, LITE) is not None
    ]
    assert booleans, "โมเดลต้องมีคอลัมน์ boolean ที่มี default ไม่งั้นเทสนี้ไม่ได้ตรวจอะไร"

    for table, column in booleans:
        where = f"{table.name}.{column.name}"
        assert _default_literal(column, PG) in ("true", "false"), where
        assert _default_literal(column, LITE) in ("1", "0"), where


def test_reserved_words_in_column_names_are_quoted():
    """บั๊กจริงตัวที่สอง: `quota_policies.window` — WINDOW เป็นคำสงวนของ Postgres

    DDL ที่ต่อสตริงเองโดยไม่ quote จะได้ syntax error · create_all ไม่เคยเจอเพราะ
    SQLAlchemy เป็นคนเขียนให้ ที่เจอคือทางอัปเกรดสคีมาซึ่งเขียน DDL เอง
    """
    column = Base.metadata.tables["quota_policies"].c["window"]
    assert PG.identifier_preparer.format_column(column) == '"window"'
    assert QuotaPolicy.window is not None  # ยังเป็นแอตทริบิวต์เดิมของ ORM


@pytest.mark.parametrize("dialect,ok_true,ok_false", [
    (PG, "DEFAULT true", "DEFAULT false"),
    (LITE, "DEFAULT 1", "DEFAULT 0"),
])
def test_schema_upgrade_ddl_is_valid_for_each_dialect(dialect, ok_true, ok_false):
    """เดิน planner ของจริงบนตารางที่มีอยู่แต่ยังไม่มีคอลัมน์ไหนเลย"""
    from app.db.session import plan_missing_columns

    statements = plan_missing_columns(_EmptyTables(dialect))
    assert statements, "ควรมีคำสั่งเติมคอลัมน์อย่างน้อยหนึ่งคำสั่ง"

    booleans = [s for s in statements if " BOOLEAN" in s and "DEFAULT" in s]
    assert booleans, "ต้องมีคอลัมน์ boolean ที่มี default ไม่งั้นเทสนี้ไม่ได้ตรวจอะไร"
    for statement in booleans:
        tail = statement.split("DEFAULT", 1)[1].strip()
        assert f"DEFAULT {tail}" in (ok_true, ok_false), statement

    users = [s for s in statements if "users" in s and "must_change_password" in s]
    assert users and users[0].endswith(ok_false), users


def test_schema_upgrade_ddl_quotes_reserved_words():
    """`window` ต้องถูก quote บน Postgres ไม่งั้น ALTER TABLE เป็น syntax error"""
    from app.db.session import plan_missing_columns

    pg = [s for s in plan_missing_columns(_EmptyTables(PG)) if "quota_policies" in s]
    assert any('ADD COLUMN "window" ' in s for s in pg), pg


class _EmptyTables:
    """connection ปลอม: ทุกตารางมีอยู่ แต่ไม่มีคอลัมน์สักคอลัมน์

    ทำให้ planner ต้องออก DDL สำหรับ *ทุก* คอลัมน์ของโมเดล ซึ่งเป็นการตรวจที่กว้าง
    กว่าการรอให้มีใครเพิ่มคอลัมน์ใหม่จริง ๆ แล้วค่อยรู้ว่ามันพังบน Postgres
    """

    def __init__(self, dialect) -> None:
        self.dialect = dialect

    def _inspector(self):
        names = [t.name for t in Base.metadata.sorted_tables]

        class _I:
            @staticmethod
            def get_table_names():
                return names

            @staticmethod
            def get_columns(_name):
                return []

        return _I()


@pytest.fixture(autouse=True)
def _inspect_fake(monkeypatch):
    """ให้ `inspect(conn)` ของ session.py คืน inspector ของ _EmptyTables"""
    import app.db.session as session_mod

    real = session_mod.inspect
    monkeypatch.setattr(
        session_mod, "inspect",
        lambda conn: conn._inspector() if isinstance(conn, _EmptyTables) else real(conn),
    )


# ── ค่าตั้ง engine ต่างกันตาม dialect โดยไม่ต้องต่อฐานข้อมูลจริง ───────────────

def test_engine_settings_match_the_dialect(monkeypatch, tmp_path):
    from app import config as config_mod
    from app.db import session as session_mod

    captured: dict = {}

    def fake_create(url, **kwargs):
        captured["url"], captured["kwargs"] = url, kwargs
        return object()

    monkeypatch.setattr(session_mod, "create_async_engine", fake_create)
    monkeypatch.setattr(session_mod, "_apply_sqlite_pragmas", lambda _e: None)

    for url, expect in (
        (f"sqlite+aiosqlite:///{tmp_path}/a.db", "sqlite"),
        ("postgresql+asyncpg://u:p@localhost:5432/db", "postgres"),
    ):
        monkeypatch.setenv("GW_DATABASE_URL", url)
        config_mod.get_settings.cache_clear()
        session_mod._engine = None
        session_mod.get_engine()
        kwargs = captured["kwargs"]
        if expect == "sqlite":
            assert "connect_args" in kwargs, "SQLite ต้องส่ง busy timeout ให้ไดรเวอร์"
            assert kwargs["pool_size"] == session_mod.SQLITE_POOL_SIZE
            assert "pool_pre_ping" not in kwargs, "ไฟล์ในเครื่องไม่มี connection ตาย"
        else:
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["pool_recycle"] == session_mod.POSTGRES_POOL_RECYCLE_SECONDS
            assert "connect_args" not in kwargs

    session_mod._engine = None
    config_mod.get_settings.cache_clear()


def test_a_missing_driver_says_how_to_install_it(monkeypatch, tmp_path):
    """ไม่มี asyncpg ต้องได้คำแนะนำ ไม่ใช่ ModuleNotFoundError ดิบ ๆ"""
    from app import config as config_mod
    from app.db import session as session_mod

    def boom(_url, **_kw):
        raise ModuleNotFoundError("No module named 'asyncpg'")

    monkeypatch.setattr(session_mod, "create_async_engine", boom)
    monkeypatch.setenv("GW_DATABASE_URL", "postgresql+asyncpg://u:pw@db.internal/litegate")
    config_mod.get_settings.cache_clear()
    session_mod._engine = None

    with pytest.raises(session_mod.DatabaseDriverMissing) as caught:
        session_mod.get_engine()

    message = str(caught.value)
    assert "litegate[postgres]" in message
    assert "pw" not in message, "อย่าพ่นรหัสผ่านของฐานข้อมูลออกมาในข้อความผิดพลาด"

    session_mod._engine = None
    config_mod.get_settings.cache_clear()


# ── พฤติกรรมจริงบน SQLite (ซึ่งเป็นค่าเริ่มต้น) ────────────────────────────────

async def test_daily_rollup_buckets_by_utc_day_on_sqlite(temp_db):
    """ของที่ compile แล้วต้องรันได้จริงด้วย ไม่ใช่แค่หน้าตาถูก"""
    from sqlalchemy import func

    from app.db.session import init_db, session_scope

    await init_db()
    # 23:30Z กับ 00:30Z ของวันถัดไป — ห่างกันชั่วโมงเดียว แต่คนละวัน timezone.utc
    late = datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
    async with session_scope() as db:
        for offset, alias in ((timedelta(0), "a"), (timedelta(hours=1), "b")):
            db.add(UsageLog(request_id=f"r{alias}", ts=late + offset,
                            model_alias=alias, protocol="openai"))

    async with session_scope() as db:
        rows = (await db.execute(
            select(utc_date(UsageLog.ts), func.count(UsageLog.id))
            .group_by(utc_date(UsageLog.ts)).order_by(utc_date(UsageLog.ts))
        )).all()

    assert [str(day) for day, _ in rows] == ["2026-03-01", "2026-03-02"]
    assert all(count == 1 for _, count in rows)


# ── ของจริง — ข้ามอย่างชัดเจนเมื่อไม่มี Postgres ────────────────────────────────

pg_only = pytest.mark.skipif(
    not PG_URL,
    reason="ตั้ง GW_TEST_POSTGRES_URL เพื่อรันกับ PostgreSQL จริง (ไม่ตั้ง = ข้าม ไม่ใช่ล้ม)",
)


@pg_only
async def test_schema_and_daily_rollup_work_on_a_real_postgres(monkeypatch):
    pytest.importorskip("asyncpg", reason="ต้องมี asyncpg: pip install 'litegate[postgres]'")

    from app import config as config_mod
    from app.db import session as session_mod

    monkeypatch.setenv("GW_DATABASE_URL", PG_URL)
    config_mod.get_settings.cache_clear()
    session_mod._engine = None
    session_mod._sessionmaker = None

    engine = session_mod.get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        # init_db สร้างตาราง แล้วเดินทางอัปเกรดสคีมาต่อ — ทางที่บั๊ก boolean เคยล้ม
        await session_mod.init_db()
        await session_mod._add_missing_columns(engine)

        late = datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
        async with session_mod.session_scope() as db:
            for offset, alias in ((timedelta(0), "a"), (timedelta(hours=1), "b")):
                db.add(UsageLog(request_id=f"pg-{alias}", ts=late + offset,
                                model_alias=alias, protocol="openai"))

        async with session_mod.session_scope() as db:
            # บังคับ TimeZone ของ session ให้ไม่ใช่ timezone.utc — ถ้าเราใช้ func.date() ตรง ๆ
            # เทสนี้จะแตกตรงนี้ ซึ่งคือจุดที่ต้องการให้แตก
            from sqlalchemy import func, text

            await db.execute(text("SET TIME ZONE 'Asia/Bangkok'"))
            rows = (await db.execute(
                select(utc_date(UsageLog.ts), func.count(UsageLog.id))
                .group_by(utc_date(UsageLog.ts)).order_by(utc_date(UsageLog.ts))
            )).all()

        assert [str(day) for day, _ in rows] == ["2026-03-01", "2026-03-02"]
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await session_mod.dispose_db()
        config_mod.get_settings.cache_clear()
