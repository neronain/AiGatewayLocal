"""ข้อผิดพลาดของฐานข้อมูลต้องไม่พาค่าที่ผูกกับคำสั่งออกไปด้วย — ไม่ว่า log ระดับไหน

เคสจริง 2026-10-09 (ผู้ตรวจอิสระพิสูจน์ด้วย trigger บน SQLite): การผนึกสำเนา key ใหม่ที่เขียน
ไม่สำเร็จ — ฐานถูกล็อก · การเชื่อมต่อ PostgreSQL หลุด · ดิสก์เต็ม — ทำให้ SQLAlchemy โยน
ข้อผิดพลาดที่มี `[parameters: ('v2:…ค่าที่ผนึกใหม่', …, 'v2:…ค่าที่ผนึกเดิม')]` อยู่ในข้อความ ·
route ส่งต่อให้ตัวจับกลางซึ่ง `log.exception` ที่ระดับ ERROR สำเนาที่ผนึกทั้งเก่าและใหม่จึงลง
journal ทั้งก้อน โดยไม่ต้องตั้ง GW_LOG_LEVEL=DEBUG เลย

ช่องเดียวกับที่ `a8c3856` ปิดให้ aiosqlite ที่ระดับ DEBUG แต่อยู่ในทางที่ทุกเครื่องเปิดอยู่เสมอ ·
แก้ที่ engine (`hide_parameters`) ไม่ใช่ที่จุดผนึกใหม่จุดเดียว เพราะคำสั่งเกือบทุกตัวของแอปนี้ผูก
hash ของ key · hash ของรหัสผ่าน · หรือสำเนาที่ผนึก ไว้ที่ใดที่หนึ่ง

ทุกข้อทำให้การเขียนล้มจริงที่ระดับไดรเวอร์ ไม่ได้จำลองข้อความของข้อผิดพลาด
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import REPO_ROOT
from tests.keyvault_kit import (
    A,
    B,
    auth,
    issued_under,
    sealed_column,
    secret_switch,
    vault,
)

pytestmark = pytest.mark.sqlite_only      # trigger ที่ใช้ทำให้เขียนล้มเป็นไวยากรณ์ของ SQLite


@pytest.fixture
def secrets(monkeypatch):
    yield from secret_switch(monkeypatch)


def writes_to_sealed_copies_fail(client) -> None:
    """ทำให้ UPDATE ของคอลัมน์ที่ผนึกล้มที่ไดรเวอร์ — ยืนแทน "database is locked" · สายหลุด · ดิสก์เต็ม"""
    from app.db.session import session_scope

    async def install():
        async with session_scope() as session:
            await session.execute(text(
                "CREATE TRIGGER no_reseal BEFORE UPDATE OF key_sealed ON api_keys "
                "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
            ))

    client.portal.call(install)


def payloads(column: dict[str, str]) -> list[str]:
    return [value.rsplit(":", 1)[-1] for value in column.values() if value]


def test_a_failing_reseal_write_logs_the_failure_but_not_the_sealed_copies(
        secrets, client, caplog, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core import keyvault

    keys = issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    secrets(B, previous=A)
    before = sealed_column(client)
    writes_to_sealed_copies_fail(client)

    # จดค่าที่ตัวผนึกจริงผลิตออกมาระหว่างรอบนี้ — ค่าใหม่ก็ต้องไม่อยู่ใน log เช่นกัน
    real_seal, fresh = keyvault.seal, []

    def remembering(plaintext: str) -> str:
        fresh.append(real_seal(plaintext))
        return fresh[-1]

    monkeypatch.setattr(keyvault, "seal", remembering)
    caplog.set_level(logging.INFO)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    response = quiet.post("/admin/key-vault/reseal", headers=auth(client.admin_key))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    # ความล้มเหลวถูกเขียนลง log จริง และยังบอกว่าเกิดอะไร — ไม่ใช่ผ่านเพราะไม่มีอะไรถูกเขียน
    assert "unhandled error on /admin/key-vault/reseal" in caplog.text
    assert "simulated write failure" in caplog.text
    assert "UPDATE api_keys" in caplog.text, "คำสั่งที่ล้มยังอยู่ให้ไล่ปัญหา"

    assert fresh, "ต้องมีค่าที่ผนึกใหม่เกิดขึ้นจริงก่อนการเขียนที่ล้ม"
    for blob in [*payloads(before), *(value.rsplit(":", 1)[-1] for value in fresh)]:
        assert blob not in caplog.text, "ค่าที่ผนึกลง log"
        assert blob not in response.text
    for created in keys:
        assert created["api_key"] not in caplog.text
    assert sealed_column(client) == before, "การเขียนที่ล้มต้องไม่ทิ้งอะไรไว้"


def test_the_command_line_does_not_print_sealed_copies_when_a_write_fails(secrets, client):
    """ทางบรรทัดคำสั่งเป็นอีก process จริง — ข้อผิดพลาดเดียวกันไปออกที่ stderr ของผู้ดูแล"""
    issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    secrets(B, previous=A)
    before = sealed_column(client)
    current_id = vault(client).json()["current_key_id"]
    writes_to_sealed_copies_fail(client)

    done = subprocess.run(
        [sys.executable, "-m", "app.tools", "keyvault", "reseal", "--yes"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        env={**os.environ, "GW_KEY_REVEAL_SECRET": B, "GW_KEY_REVEAL_SECRET_PREVIOUS": A},
    )
    said = done.stdout + done.stderr
    assert done.returncode != 0
    assert "simulated write failure" in said, "ต้องบอกว่าล้มเพราะอะไร"
    for blob in payloads(before):
        assert blob not in said
    assert f"v2:{current_id}:" not in said, "ค่าที่เพิ่งผนึกใหม่ (ขึ้นต้นด้วยป้ายของ secret ปัจจุบัน)"
    assert sealed_column(client) == before


def test_any_failing_statement_keeps_its_bound_values_out_of_the_error(secrets, client):
    """ไม่ใช่แค่การผนึกใหม่: hash ของ key ที่ชนกันก็ต้องไม่อยู่ในข้อความของข้อผิดพลาด

    สิ่งที่ยังอยู่ให้ไล่ปัญหา: ชนิดของข้อผิดพลาด · ข้อความของไดรเวอร์ · ตัวคำสั่ง SQL —
    โค้ดโควตาอ่านคำว่า "locked" จากข้อความนี้เพื่อลองใหม่ จึงต้องไม่หายไปด้วย
    """
    from sqlalchemy.exc import IntegrityError

    from app.db.models import ApiKey
    from app.db.session import session_scope

    (created,) = issued_under(client, secrets, A, 1, prefix="k", fmt="new")
    stored = sealed_column(client)[created["id"]]

    async def duplicate():
        from sqlalchemy import select

        async with session_scope() as session:
            row = (await session.execute(
                select(ApiKey).where(ApiKey.id == created["id"]))).scalar_one()
            taken = row.key_hash
            session.add(ApiKey(user_id=row.user_id, name="dup", key_prefix=row.key_prefix,
                               key_hash=taken, key_sealed=stored))
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                return taken, str(exc)
        return taken, ""

    taken, message = client.portal.call(duplicate)
    assert "UNIQUE constraint failed" in message, "ข้อความของไดรเวอร์ยังอยู่"
    assert "INSERT INTO api_keys" in message, "ตัวคำสั่งยังอยู่"
    assert taken not in message, "hash ของ key"
    assert stored.rsplit(":", 1)[-1] not in message, "ค่าที่ผนึก"
