"""Shared fixtures. Every test runs against a fresh in-file SQLite database."""

from __future__ import annotations

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


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return REPO_ROOT / "config"


@pytest.fixture
def temp_db(monkeypatch):
    """Isolated database per test, with all caches reset."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("GW_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.setenv("GW_CONFIG_DIR", str(REPO_ROOT / "config"))

        from app import config as config_mod
        from app.db import session as session_mod

        config_mod.get_settings.cache_clear()
        session_mod._engine = None
        session_mod._sessionmaker = None
        yield db_path
        session_mod._engine = None
        session_mod._sessionmaker = None
        config_mod.get_settings.cache_clear()


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
        test_client.admin_key = _bootstrap_key(app)
        yield test_client


def _bootstrap_key(app) -> str:
    """Mint a known admin key directly, so tests never scrape the log."""
    import asyncio

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

    return asyncio.run(_create())


@pytest.fixture
def student_key(client):
    """A student account with a key, created through the admin API."""
    headers = {"Authorization": f"Bearer {client.admin_key}"}
    user = client.post(
        "/admin/users",
        json={"external_id": "6412345678", "display_name": "Somchai", "role": "student"},
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
