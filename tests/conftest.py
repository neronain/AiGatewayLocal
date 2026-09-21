"""Shared fixtures. Every test runs against a fresh, empty database.

ค่าเริ่มต้นคือ SQLite: ไฟล์ชั่วคราวใหม่ต่อเทสหนึ่งตัว — ลงแล้วรัน `pytest` ได้เลย
โดยไม่ต้องมีเซิร์ฟเวอร์ฐานข้อมูล และพฤติกรรมของเทสทุกตัวเป็นอย่างที่เคยเป็น

ตั้ง ``GW_TEST_POSTGRES_URL`` แล้ว **ชุดเดียวกันนี้ทั้งชุด** จะวิ่งบน PostgreSQL จริง
แทน · ไม่ตั้ง = ไม่มีอะไรเปลี่ยน (ไม่ใช่ "ล้มเพราะหา Postgres ไม่เจอ")

    GW_TEST_POSTGRES_URL=postgresql+asyncpg://litegate@127.0.0.1:5432/litegate_test \\
      .venv/bin/python -m pytest -q

⚠️ ฐานข้อมูลที่ชี้ไปจะถูกล้างทุกตารางก่อนทุกเทส — ชี้ไปที่ฐานข้อมูลสำหรับเทสเท่านั้น

มีสองเรื่องที่เคยทำให้ชุดเทสนี้วิ่งบน Postgres ไม่ได้เลย ทั้งคู่แก้ไว้ที่ไฟล์นี้:

**1. การแยกกันของเทส** — บน SQLite เราใช้ "ไฟล์ใหม่ต่อเทส" ซึ่งบน Postgres ทำแบบ
เดียวกันไม่ได้ (CREATE DATABASE ต่อเทสช้าเกินกว่าจะใช้จริงกับเทสเกือบแปดร้อยตัว)
แทนที่ด้วย ``TRUNCATE ... RESTART IDENTITY CASCADE`` ทุกตารางก่อนทุกเทส ซึ่งให้ผล
อย่างเดียวกับไฟล์ใหม่: ไม่มีแถวไหนเหลือข้ามเทส และเป็นสิ่งที่ *ตรวจได้* ไม่ใช่
สิ่งที่ต้องเชื่อ (ดู "พิสูจน์" ข้างล่าง)

จงใจ **ไม่** ใช้แนวทาง "เปิด transaction ครอบแล้ว rollback ต่อเทส" ที่เป็นสูตร
สำเร็จทั่วไป เพราะกับเกตเวย์ตัวนี้มันพิสูจน์ไม่ได้ว่าแยกกันจริง: โค้ดของจริง
commit เองหลายที่ (``session_scope``, ``release_connection`` ที่ commit กลางคำขอ
เพื่อคืน connection ระหว่างสตรีม, งานพื้นหลังที่เปิด session ของตัวเองจาก
sessionmaker) การจะ rollback ทับของพวกนั้นได้ต้องบังคับให้ทุก session ไปเกาะ
connection เดียวกัน ซึ่งขัดกับสิ่งที่ ``test_streaming_connection_release`` ตรวจอยู่
พอดี · TRUNCATE ไม่ต้องเชื่ออะไรเลย: หลังจากนั้นตารางว่างจริง ใครจะ commit ไปแล้ว
ก็ตาม

*วิธีตรวจว่ามันแยกกันจริง* — เขียนเทสคู่หนึ่งไว้ท้ายชุด: ตัวแรกเขียนข้อมูลลงทุก
ตารางแล้ว commit ตัวที่สองนับทุกตารางว่าต้องเป็นศูนย์และใส่ ``users.external_id``
ค่าเดิมซ้ำโดยไม่ชน unique constraint · ต้องได้ผลเหมือนกันทั้งสองฝั่ง และเมื่อปิด
บรรทัด ``_truncate_everything()`` ข้างล่างทิ้ง เทสคู่นั้นต้องล้มทันที — ถ้าไม่ล้ม
แปลว่ามันไม่ได้ตรวจอะไรอยู่ · เช็กแบบนี้ก่อนแก้อะไรที่เกี่ยวกับ fixture ชุดนี้

**2. event loop** — asyncpg ผูก connection ไว้กับ event loop ที่สร้างมัน ส่วนชุดเทส
นี้มี loop มากกว่าหนึ่งอันต่อเทสโดยธรรมชาติ: ``TestClient`` รัน ASGI app บน loop
ของ portal ในอีกเธรด ขณะที่ตัวเทสเองเรียก ``asyncio.run(...)`` เพื่อ seed ข้อมูล
อยู่อีกอันหนึ่ง (มีแบบนี้อีกยี่สิบกว่าจุดทั่วชุดเทส) · aiosqlite ไม่สนเรื่องนี้เพราะ
มันเปิดเธรดต่อ connection แต่ asyncpg ระเบิดทันทีที่ connection จาก pool ถูกหยิบไป
ใช้ข้าม loop

แก้ที่ระดับ pool แทนที่จะไล่แก้ทุกจุดที่เรียก ``asyncio.run``: ติดป้าย loop ให้
connection ตอนเปิด แล้วตอน checkout ถ้าเป็นคนละ loop ก็โยน ``DisconnectionError``
ซึ่ง SQLAlchemy ตีความว่า "connection นี้ใช้ไม่ได้แล้ว" แล้วเปลี่ยนตัวใหม่ให้เงียบ ๆ
— กลไกเดียวกับที่ใช้รับมือ connection ที่ถูกไฟร์วอลล์ตัดทิ้ง ต่างกันแค่เหตุผล

ทั้งสองอย่างข้างบนติดตั้งเฉพาะตอนรันบน Postgres เท่านั้น · ทางของ SQLite ข้างล่าง
เป็นโค้ดเดิมคำต่อคำ
"""

from __future__ import annotations

import asyncio
import base64
import os
import struct
import tempfile
import zlib
from pathlib import Path

import pytest

os.environ.setdefault("GW_REGISTRY_RELOAD_SECONDS", "0")
os.environ.setdefault("GW_API_KEY_PEPPER", "test-pepper-not-secret")
os.environ.setdefault("GW_CONFIG_DIR", "./config")

REPO_ROOT = Path(__file__).resolve().parent.parent

# ตัวเดียวกับที่ tests/test_postgres_support.py ใช้เปิดเทสของจริงของมัน — ตั้งตัวเดียว
# ได้ทั้งสองอย่าง ไม่ต้องจำสองชื่อ
POSTGRES_URL = os.environ.get("GW_TEST_POSTGRES_URL", "").strip()
ON_POSTGRES = bool(POSTGRES_URL)


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return REPO_ROOT / "config"


# ── สวิตช์ SQLite / PostgreSQL ────────────────────────────────────────────────

def pytest_configure(config) -> None:  # noqa: ANN001
    config.addinivalue_line(
        "markers",
        "sqlite_only: ตรวจพฤติกรรมที่เป็นของ SQLite โดยเฉพาะ — ข้ามเมื่อรันบน PostgreSQL",
    )


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ANN001
    """บนฐานข้อมูลอื่น เทสที่ตรวจเรื่องเฉพาะของ SQLite ต้อง *ข้าม* ไม่ใช่ล้ม

    เทสพวกนี้ไม่ได้ผิด และไม่ได้บอกอะไรเกี่ยวกับ Postgres — มันตรวจ WAL, busy_timeout
    และขนาด pool ของไฟล์ในเครื่อง ซึ่งเป็นคนละเรื่องกันโดยสิ้นเชิง
    """
    if not ON_POSTGRES:
        return
    skip = pytest.mark.skip(reason="พฤติกรรมเฉพาะของ SQLite — ไม่เกี่ยวกับ PostgreSQL")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


def _reset_caches() -> None:
    """ลืมทุกอย่างที่ cache ไว้ระหว่างเทส — Settings และ engine/sessionmaker"""
    from app import config as config_mod
    from app.db import session as session_mod

    config_mod.get_settings.cache_clear()
    session_mod._engine = None
    session_mod._sessionmaker = None


@pytest.fixture
def temp_db(monkeypatch):
    """Isolated database per test, with all caches reset."""
    if ON_POSTGRES:
        yield from _postgres_db(monkeypatch)
    else:
        yield from _sqlite_db(monkeypatch)


def _sqlite_db(monkeypatch):
    """ไฟล์ใหม่ต่อเทส · นี่คือค่าเริ่มต้น และเป็นโค้ดเดิมทุกบรรทัด"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("GW_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("GW_CONFIG_DIR", str(REPO_ROOT / "config"))
        _reset_caches()
        yield db_path
        _reset_caches()


def _postgres_db(monkeypatch):
    """ฐานข้อมูลเดิม แต่ว่างเปล่าเหมือนเพิ่งสร้าง — ดู docstring ของไฟล์ ข้อ 1

    engine ถูกแชร์ทั้งรัน (URL ไม่เคยเปลี่ยน) แทนที่จะสร้างใหม่ต่อเทสแบบฝั่ง SQLite:
    สร้างใหม่ทุกเทสแปลว่าทิ้ง pool เก่าทั้งที่ยังถือ connection ของ Postgres อยู่ เกือบ
    แปดร้อยรอบ ซึ่งชน max_connections ของเซิร์ฟเวอร์ก่อนจะรันจบ
    """
    from app.db import session as session_mod

    engine, sessionmaker = _postgres_backend()

    monkeypatch.setenv("GW_DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("GW_CONFIG_DIR", str(REPO_ROOT / "config"))
    _reset_caches()
    # ใส่กลับทุกเทส เพราะมีเทสที่ตั้ง _engine = None เองระหว่างทาง (และควรตั้งได้)
    session_mod._engine = engine
    session_mod._sessionmaker = sessionmaker

    asyncio.run(_truncate_everything())
    yield POSTGRES_URL
    _reset_caches()
    session_mod._engine = engine
    session_mod._sessionmaker = sessionmaker


# ── เครื่องมือฝั่ง PostgreSQL ─────────────────────────────────────────────────

_BACKEND: dict = {}


def _postgres_backend():
    """engine ตัวเดียวของทั้งรัน พร้อมกลไกกัน connection ข้าม event loop"""
    if "engine" not in _BACKEND:
        from app import config as config_mod
        from app.db import session as session_mod

        previous = os.environ.get("GW_DATABASE_URL")
        os.environ["GW_DATABASE_URL"] = POSTGRES_URL
        try:
            config_mod.get_settings.cache_clear()
            session_mod._engine = None
            session_mod._sessionmaker = None
            # ผ่าน get_engine() ของจริง เพื่อให้ค่า pool ที่ใช้ตอนเทสเป็นค่าเดียวกับที่
            # production ใช้ ไม่ใช่ค่าที่ไฟล์นี้คิดขึ้นเอง
            engine = session_mod.get_engine()
            _guard_against_cross_loop_reuse(engine)
            sessionmaker = session_mod.get_sessionmaker()
        finally:
            if previous is None:
                os.environ.pop("GW_DATABASE_URL", None)
            else:
                os.environ["GW_DATABASE_URL"] = previous
            config_mod.get_settings.cache_clear()

        asyncio.run(_create_schema())
        _BACKEND["engine"] = engine
        _BACKEND["sessionmaker"] = sessionmaker
    return _BACKEND["engine"], _BACKEND["sessionmaker"]


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:  # ถูกเรียกจาก sync context ล้วน ๆ
        return None


def _guard_against_cross_loop_reuse(engine) -> None:  # noqa: ANN001
    """ห้าม connection ของ asyncpg ถูกหยิบข้าม event loop — ดู docstring ของไฟล์ ข้อ 2"""
    from sqlalchemy import event
    from sqlalchemy.exc import DisconnectionError

    # pre_ping ทำงาน *ก่อน* event checkout และตัวมันเองคือการยิง SELECT 1 ผ่าน
    # connection เดิม — ถ้า connection นั้นเป็นของ loop อื่นก็ระเบิดตรงนั้นเลย ก่อนที่
    # ยามข้างล่างจะได้ทำงาน · ของจริงต้องมี pre_ping (connection วิ่งข้ามเน็ตและถูกตัดได้)
    # แต่ในเทสฐานข้อมูลอยู่ที่ localhost และไม่มีใครมาตัดสาย
    engine.pool._pre_ping = False

    @event.listens_for(engine.sync_engine, "connect")
    def _remember_loop(_dbapi_connection, record) -> None:  # noqa: ANN001
        record.info["loop"] = _running_loop()

    @event.listens_for(engine.sync_engine, "checkout")
    def _refuse_foreign_loop(_dbapi_connection, record, _proxy) -> None:  # noqa: ANN001
        loop = _running_loop()
        if loop is not None and record.info.get("loop") is not loop:
            raise DisconnectionError("pooled connection belongs to another event loop")


def _asyncpg_dsn() -> str:
    """URL เดียวกันในรูปที่ asyncpg รับตรง ๆ (ไม่ผ่าน SQLAlchemy)"""
    from sqlalchemy.engine import make_url

    url = make_url(POSTGRES_URL).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


async def _create_schema() -> None:
    """สร้างตารางทั้งหมด ด้วย engine ใช้แล้วทิ้งที่ไม่เก็บ connection ไว้ใน pool"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.models import Base

    engine = create_async_engine(POSTGRES_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    finally:
        await engine.dispose()


async def _truncate_everything() -> None:
    """ล้างทุกตารางและรีเซ็ต sequence ของ id — เทียบเท่าไฟล์ SQLite ที่เพิ่งสร้าง

    คุยกับ asyncpg ตรง ๆ ไม่ผ่าน pool ของ engine ที่แชร์อยู่ เพราะฟังก์ชันนี้ถูกเรียก
    จาก ``asyncio.run`` ที่ loop ของมันตายทันทีที่จบ — ยืม connection จาก pool ตรงนี้
    ก็เท่ากับทิ้ง connection ที่ผูกกับ loop ที่ตายแล้วไว้ให้เทสถัดไปเจอ

    CASCADE ใส่ไว้เผื่อมีตารางนอกโมเดลอ้างถึงตารางพวกนี้อยู่ (ตารางในลิสต์อ้างกันเอง
    ได้อยู่แล้วโดยไม่ต้องมี) · RESTART IDENTITY ตอนนี้ไม่ได้ทำอะไรเพราะ primary key
    ทุกตัวเป็นสตริงสุ่ม ไม่ใช่ sequence — เก็บไว้ให้ยังถูกในวันที่มีใครเพิ่มคอลัมน์ที่
    นับเอง แล้วเทสเริ่มคาดหวังว่าแถวแรกคือ 1
    """
    import asyncpg

    from app.db.models import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    statement = f"TRUNCATE {tables} RESTART IDENTITY CASCADE"

    connection = await asyncpg.connect(_asyncpg_dsn())
    try:
        try:
            await connection.execute(statement)
        except asyncpg.exceptions.UndefinedTableError:
            # test_postgres_support.py ทำ drop_all ของมันเองตอนจบ — สร้างคืนแล้วลองใหม่
            await connection.close()
            await _create_schema()
            connection = await asyncpg.connect(_asyncpg_dsn())
            await connection.execute(statement)
    finally:
        if not connection.is_closed():
            await connection.close()


@pytest.fixture
def writable_config(temp_db, monkeypatch, tmp_path):
    """A throwaway copy of config/, so save/delete tests never touch the repo.

    Depends on temp_db and must be requested BEFORE `client` in a test's
    parameter list: the app captures Settings once at startup, so the override
    has to be in place before the client fixture builds the app. Getting this
    order wrong silently writes into the real repo config.
    """
    import shutil

    from app import config as config_mod

    target = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", target)
    monkeypatch.setenv("GW_CONFIG_DIR", str(target))
    config_mod.get_settings.cache_clear()
    yield target
    config_mod.get_settings.cache_clear()


@pytest.fixture
def client(temp_db):
    """TestClient with lifespan run, plus the bootstrap admin key."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        test_client.admin_key = _bootstrap_key(test_client)
        yield test_client


def _bootstrap_key(test_client) -> str:  # noqa: ANN001
    """Mint a known admin key directly, so tests never scrape the log.

    รันบน event loop ของ ``TestClient`` เอง ไม่ใช่ ``asyncio.run`` ของตัวเอง · lifespan
    เพิ่งเปิด engine ขึ้นมาบน loop นั้น และ connection ของ asyncpg ผูกอยู่กับ loop ที่
    สร้างมัน — การยืมมาใช้จาก loop ที่สองคือจุดที่ชุดเทสนี้เคยระเบิดบน Postgres
    """
    from app.core.auth import generate_api_key
    from app.db.models import ApiKey, User
    from app.db.session import session_scope

    async def _create() -> str:
        plaintext, prefix, digest = generate_api_key()
        async with session_scope() as session:
            user = User(
                external_id="test-admin", display_name="Test Admin", role="admin"
            )
            session.add(user)
            await session.flush()
            session.add(
                ApiKey(
                    user_id=user.id,
                    name="test",
                    key_prefix=prefix,
                    key_hash=digest,
                    scopes=[],
                )
            )
        return plaintext

    return test_client.portal.call(_create)


@pytest.fixture
def member_key(client):
    """A member account with a key, created through the admin API."""
    headers = {"Authorization": f"Bearer {client.admin_key}"}
    user = client.post(
        "/admin/users",
        json={"external_id": "6412345678", "display_name": "Somchai", "role": "member"},
        headers=headers,
    ).json()
    key = client.post(
        "/admin/api-keys",
        json={"user_id": user["id"], "name": "lab"},
        headers=headers,
    ).json()
    return key["api_key"]


def make_png(width: int = 64, height: int = 64) -> bytes:
    """Minimal valid PNG with a correct IHDR, so header parsing is exercised."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def png_data_url(width: int = 64, height: int = 64) -> str:
    return "data:image/png;base64," + base64.b64encode(make_png(width, height)).decode()
