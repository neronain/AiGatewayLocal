"""Async engine / session management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.exc import InternalError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.models import Base

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite"):
        path = url.split("///")[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)


# SQLite ในโหมดปริยาย (journal_mode=delete) ล็อกทั้งไฟล์ตอนเขียน · เกตเวย์รัน
# uvicorn หลาย worker บนไฟล์เดียว การเขียนสองที่พร้อมกันจึงชน และคำขอที่แพ้จะได้
# `database is locked` ซึ่งโผล่ออกไปเป็น HTTP 500 ทั้งที่ backend ไม่ได้ผิดอะไร
#
# WAL ให้คนอ่านกับคนเขียนอยู่ร่วมกันได้ และ busy_timeout สั่งให้รอแทนที่จะยอมแพ้ทันที
# ตั้งที่ระดับ connection เพราะ journal_mode ติดอยู่กับไฟล์ แต่ busy_timeout ติดอยู่กับ
# connection — ตั้งครั้งเดียวตอนสร้าง engine ไม่พอ
SQLITE_BUSY_TIMEOUT_MS = 15000


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Make SQLite survive more than one worker writing at once."""

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        except Exception:  # ไฟล์อ่านอย่างเดียวหรือ :memory: — ปล่อยให้เดินต่อ
            log.debug("could not apply sqlite pragmas", exc_info=True)
        finally:
            cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _ensure_sqlite_dir(settings.database_url)
        kwargs: dict = {"echo": False, "future": True}
        is_sqlite = settings.database_url.startswith("sqlite")
        if is_sqlite:
            # ให้ไดรเวอร์รอล็อกด้วย ไม่ใช่แค่ตัว SQLite เอง
            kwargs["connect_args"] = {"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000}
        else:
            kwargs.update(pool_size=20, max_overflow=10, pool_pre_ping=True)
        _engine = create_async_engine(settings.database_url, **kwargs)
        if is_sqlite:
            _apply_sqlite_pragmas(_engine)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background work and startup tasks."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with get_sessionmaker()() as session:
        yield session


async def init_db(attempts: int = 5) -> None:
    """Create tables if missing.

    Every uvicorn worker runs this at startup against the same database, so
    `create_all`'s check-then-create is a race: two workers can both see a table
    missing and both try to create it, and the loser gets "table already exists".
    The end state is identical either way, so we retry and then verify the schema
    is present rather than failing the worker.

    Schema evolution beyond MVP is handled by a migration step (see
    docs/DEPLOYMENT.md); create_all only ever adds missing tables.
    """
    engine = get_engine()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all, checkfirst=True)
            await _add_missing_columns(engine)
            log.info("database ready: %s", get_settings().database_url.split("@")[-1])
            return
        except (OperationalError, ProgrammingError, InternalError) as exc:
            last_error = exc
            if await _schema_present(engine):
                # Another worker won the race and finished the job.
                log.info("database already initialised by another worker")
                return
            # Stagger retries so workers do not collide again immediately.
            await asyncio.sleep(0.2 * attempt)

    raise RuntimeError(
        f"could not initialise the database after {attempts} attempts"
    ) from last_error



async def _add_missing_columns(engine: AsyncEngine) -> None:
    """Add columns the code expects but the database does not have yet.

    `create_all` only ever creates missing *tables*. A release that adds a
    column to an existing table - console passwords, for instance - would
    otherwise start and then fail on the first query with "no such column",
    which is a confusing way to learn you needed a migration.

    Additive only: this never drops, renames or retypes anything, so it cannot
    lose data. Anything beyond adding a column is a real migration and belongs
    in a reviewed script.
    """
    def _plan(sync_conn) -> list[str]:  # noqa: ANN001
        inspector = inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        statements: list[str] = []
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.default is None:
                    log.error(
                        "cannot add required column %s.%s automatically - "
                        "it needs a migration with a backfill",
                        table.name, column.name,
                    )
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                ddl += column.type.compile(sync_conn.dialect)
                default = getattr(column.default, "arg", None)
                if default is not None and not callable(default):
                    literal = f"'{default}'" if isinstance(default, str) else int(default) \
                        if isinstance(default, bool) else default
                    ddl += f" DEFAULT {literal}"
                statements.append(ddl)
        return statements

    async with engine.begin() as conn:
        statements = await conn.run_sync(_plan)
        for statement in statements:
            log.warning("schema upgrade: %s", statement)
            await conn.execute(text(statement))

async def _schema_present(engine: AsyncEngine) -> bool:
    """True when every expected table exists."""
    expected = set(Base.metadata.tables)
    try:
        async with engine.connect() as conn:
            found = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        return expected.issubset(found)
    except Exception:
        return False


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
