"""คีย์ของผู้ให้บริการปลายทาง — ตั้งจากหน้าเว็บได้ และห้ามรั่วกลับออกมา"""

from __future__ import annotations

import json
import os
import stat

import pytest

from app.core.secrets import SecretStore, SecretStoreError


def test_a_stored_key_is_resolved(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    assert store.resolve("MINIMAX_API_KEY") == ""
    store.set("MINIMAX_API_KEY", "sk-cp-abc")
    assert store.resolve("MINIMAX_API_KEY") == "sk-cp-abc"


def test_the_environment_wins(tmp_path, monkeypatch):
    """เครื่องที่ตั้งคีย์ผ่าน systemd/.env ไว้อยู่แล้วต้องทำงานเหมือนเดิมทุกประการ"""
    store = SecretStore(tmp_path / "secrets.json")
    store.set("GEMINI_API_KEY", "from-store")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert store.resolve("GEMINI_API_KEY") == "from-env"
    assert store.status(["GEMINI_API_KEY"])[0]["source"] == "env"


def test_the_file_is_not_readable_by_anyone_else(tmp_path):
    path = tmp_path / "secrets.json"
    SecretStore(path).set("K", "v")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_name_that_is_really_a_key_is_refused(tmp_path):
    """ช่องชื่อเคยรับคีย์จริงเข้าไปเงียบ ๆ แล้วคำขอออกไปโดยไม่มีคีย์"""
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "s.json").set("sk-cp-abc-def", "x")


def test_another_worker_sees_a_key_set_by_the_first(tmp_path):
    """เกตเวย์รันหลาย worker · คนตั้งค่าลงไปที่ตัวเดียว ตัวอื่นต้องใช้ได้ด้วย"""
    path = tmp_path / "secrets.json"
    worker_a, worker_b = SecretStore(path), SecretStore(path)
    assert worker_b.resolve("K") == ""
    worker_a.set("K", "v")
    assert worker_b.resolve("K") == "v"


def test_the_api_never_returns_the_value(client):
    key = "TEST_PROVIDER_SECRET"
    headers = {"Authorization": f"Bearer {client.admin_key}"}
    assert client.put(
        f"/admin/secrets/{key}", json={"value": "sk-do-not-leak"}, headers=headers
    ).status_code == 200

    listed = client.get("/admin/secrets", headers=headers)
    assert listed.status_code == 200
    assert "sk-do-not-leak" not in listed.text

    # ...และไม่โผล่ที่อื่นในแผงผู้ดูแลด้วย
    for path in ("/admin/models", "/admin/providers"):
        assert "sk-do-not-leak" not in client.get(path, headers=headers).text


def test_only_an_admin_may_set_a_key(client, member_key):
    response = client.put(
        "/admin/secrets/ANYTHING",
        json={"value": "x"},
        headers={"Authorization": f"Bearer {member_key}"},
    )
    assert response.status_code == 403


def test_upstream_uses_a_stored_key(tmp_path, monkeypatch):
    """เส้นทางที่ยิงจริงต้องอ่านคีย์ที่ตั้งจากหน้าเว็บ ไม่ใช่แค่ os.environ"""
    from app.registry.schema import Endpoint
    from app.upstream import client as upstream

    path = tmp_path / "secrets.json"
    SecretStore(path).set("MINIMAX_API_KEY", "sk-stored")
    monkeypatch.setattr(upstream, "_secret_store", SecretStore(path))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    endpoint = Endpoint(
        name="m", server_type="minimax", base_url="https://api.minimax.io/v1",
        api_key_env="MINIMAX_API_KEY",
    )
    headers = {k.lower(): v for k, v in upstream.upstream_headers(endpoint, {}).items()}
    assert headers["authorization"] == "Bearer sk-stored"
