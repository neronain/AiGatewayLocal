"""Async engine / session management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import inspect
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


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _ensure_sqlite_dir(settings.database_url)
        kwargs: dict = {"echo": False, "future": True}
        if not settings.database_url.startswith("sqlite"):
            kwargs.update(pool_size=20, max_overflow=10, pool_pre_ping=True)
        _engine = create_async_engine(settings.database_url, **kwargs)
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
