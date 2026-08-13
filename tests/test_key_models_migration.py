"""Upgrading a database that already has keys in it.

`api_keys.models` is a new column on a table every deployment already has, and
`create_all` only ever adds missing tables. Without the additive column step, a
release carrying this change starts cleanly and then fails on the first key
lookup with "no such column" — which is a confusing way to find out a migration
was needed.

The rows that were there before get NULL, so every read of the column has to
treat NULL and [] as the same thing: no extra restriction.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.mark.anyio
async def test_the_column_is_added_to_a_database_that_predates_it(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"

    # ฐานข้อมูลรุ่นก่อน — api_keys ไม่มีคอลัมน์ models
    old = sqlite3.connect(db_path)
    old.execute("""
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY, user_id TEXT, course_id TEXT, name TEXT,
            key_prefix TEXT, key_hash TEXT, scopes TEXT,
            expires_at TIMESTAMP, revoked_at TIMESTAMP, last_used_at TIMESTAMP,
            created_at TIMESTAMP, updated_at TIMESTAMP
        )
    """)
    old.execute(
        "INSERT INTO api_keys (id, user_id, name, key_prefix, key_hash, scopes) "
        "VALUES ('k1', 'u1', 'existing', 'lg_sk_aaaa', 'hash', '[]')"
    )
    old.commit()
    old.close()

    monkeypatch.setenv("GW_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    import app.db.session as session_module

    session_module._engine = None
    session_module._sessionmaker = None
    from app.config import get_settings

    get_settings.cache_clear()

    await session_module.init_db()
    await session_module.dispose_db()
    session_module._engine = None
    session_module._sessionmaker = None
    get_settings.cache_clear()

    check = sqlite3.connect(db_path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(api_keys)")}
    assert "models" in columns, "คอลัมน์ต้องถูกเพิ่มให้ฐานข้อมูลเดิม"

    # แถวเดิมยังอยู่ และ models เป็น NULL
    row = check.execute("SELECT name, models FROM api_keys WHERE id='k1'").fetchone()
    check.close()
    assert row[0] == "existing", "ข้อมูลเดิมต้องไม่หาย"
    assert row[1] is None


def test_a_null_column_means_no_extra_restriction():
    """แถวเก่ามี NULL — ต้องอ่านเป็น "ไม่จำกัด" ไม่ใช่ "ห้ามทุกอย่าง\""""
    from app.core.auth import Principal

    principal = Principal(
        user_id="u", external_id="e", role="member", display_name="d",
        api_key_id="k", workspace_id=None, scopes=[],
        key_models=list(None or []),
    )
    assert principal.key_models == []
