"""SQLite must survive more than one worker writing at once.

The gateway runs several uvicorn workers against one SQLite file. In SQLite's
default journal mode a write locks the whole file, so two workers writing at the
same moment make one of them fail with ``database is locked`` — which surfaces to
the caller as a 500 while every backend is perfectly healthy.

Seen in production on 2026-08-27: authentication updates ``last_used_at`` on the
key, two concurrent calls collided, and the request died in ``auth.authenticate``
before routing ever happened.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest


@pytest.mark.anyio
async def test_engine_puts_sqlite_in_wal_mode(temp_db):
    """A file-backed database is opened in WAL, not the default rollback journal."""
    from sqlalchemy import text

    from app.db.session import get_engine

    async with get_engine().connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()

    assert str(mode).lower() == "wal"
    assert int(busy) >= 1000, "a zero busy_timeout gives up on the first contended write"


def test_wal_lets_a_reader_work_while_a_writer_holds_a_transaction(tmp_path):
    """The property the pragma buys us, stated as behaviour rather than settings.

    In the default journal mode this reader raises ``database is locked``.
    """
    path = tmp_path / "concurrent.db"

    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    setup.execute("INSERT INTO t (v) VALUES ('before')")
    setup.commit()
    setup.close()

    writing = threading.Event()
    release = threading.Event()

    def hold_a_write() -> None:
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t (v) VALUES ('during')")
        writing.set()
        release.wait(timeout=5)
        conn.commit()
        conn.close()

    writer = threading.Thread(target=hold_a_write)
    writer.start()
    try:
        assert writing.wait(timeout=5), "writer never took its lock"
        reader = sqlite3.connect(path, timeout=5)
        try:
            rows = reader.execute("SELECT v FROM t").fetchall()
        finally:
            reader.close()
        # The reader sees the committed row and is not blocked by the open write.
        assert rows == [("before",)]
    finally:
        release.set()
        writer.join(timeout=5)
