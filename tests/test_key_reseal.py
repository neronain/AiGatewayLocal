"""ผนึกใหม่: ย้ายสำเนาที่ยังอยู่ใต้ secret เก่ามาอยู่ใต้ตัวปัจจุบัน เพื่อให้เอาตัวเก่าออกได้

เป็นขั้นที่ผู้ดูแล **สั่งเอง** (ปุ่มในคอนโซล หรือ `python -m app.tools keyvault reseal`)
ไม่ใช่สิ่งที่เกิดเองตอนเริ่ม process — เกตเวย์จริงรัน 4 worker ที่เริ่มพร้อมกัน และการย้าย
ของที่ผนึกไปอยู่ใต้ secret อีกตัวคือการเปลี่ยนท่าทีความปลอดภัย ซึ่งควรมีชื่อคนทำและเวลา

สิ่งที่ต้องจริง ไม่ว่าจะรันกี่ครั้ง ถูกตัดกลางทาง หรือมีสองคนกดพร้อมกัน:
ทุกแถวเป็น "ยังใต้ตัวเก่า" หรือ "ใต้ตัวใหม่แล้ว" อย่างใดอย่างหนึ่ง และเปิดได้เสมอ ไม่มีแถวไหนหาย
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from tests.conftest import REPO_ROOT
from tests.keyvault_kit import (
    A,
    B,
    C,
    auth,
    issue,
    issued_under,
    listed,
    opens,
    person,
    reseal,
    reveal,
    sealed_column,
    secret_switch,
    vault,
)


@pytest.fixture
def secrets(monkeypatch):
    yield from secret_switch(monkeypatch)


def old_rows(client, secrets):
    """ฐานแบบที่จะเจอจริงตอนเปลี่ยน: แถว `v1` จากรุ่นที่ deploy อยู่ ปนกับแถวของรุ่นนี้"""
    return [
        *issued_under(client, secrets, A, 3, prefix="v1-", fmt="v1"),
        *issued_under(client, secrets, A, 2, prefix="new-", fmt="new"),
    ]


# ── เดินทั้งเส้น ────────────────────────────────────────────────────────────

def test_after_a_reseal_the_previous_secret_is_no_longer_needed(secrets, client):
    """ตั้ง previous+current → ผนึกใหม่ → เอา previous ออก: ทุกใบยังเปิดได้ตลอดทาง"""
    keys = old_rows(client, secrets)
    assert all(opens(client, k) for k in keys), "ก่อนเปลี่ยน"

    secrets(B, previous=A)
    assert all(opens(client, k) for k in keys), "ตั้งสองตัวแล้ว ยังไม่ผนึกใหม่"
    mid = issue(client, name="issued-mid-rotation")

    done = reseal(client)
    assert done.status_code == 200, done.text
    assert done.json()["resealed"] == 5
    assert done.json()["already_current"] == 1          # ใบที่ออกระหว่างเปลี่ยน
    assert all(opens(client, k) for k in [*keys, mid]), "ผนึกใหม่แล้ว ตัวเก่ายังตั้งอยู่"

    secrets(B)
    assert all(opens(client, k) for k in [*keys, mid]), "เอาตัวเก่าออกแล้ว"
    assert all(row["seal_state"] == "current" for row in listed(client).values()
               if row["seal_state"] != "none")


def test_the_answer_says_what_is_left_to_do(secrets, client):
    """ผลของการกดต้องบอกสถานะหลังกด — ผู้ดูแลจะได้รู้ว่าเอา previous ออกได้หรือยัง"""
    old_rows(client, secrets)
    secrets(B, previous=A)

    after = reseal(client).json()["vault"]
    assert after["counts"] == {"current": 5, "previous": 0, "lost": 0, "off": 0}
    codes = {w["code"] for w in after["warnings"]}
    assert "previous_unused" in codes and "rotation_pending" not in codes


def test_a_second_run_changes_nothing(secrets, client):
    keys = old_rows(client, secrets)
    secrets(B, previous=A)

    assert reseal(client).json()["resealed"] == 5
    between = sealed_column(client)
    again = reseal(client).json()
    assert again["resealed"] == 0 and again["already_current"] == 5
    assert sealed_column(client) == between, "รอบสองต้องไม่เขียนอะไรเลย แม้แต่ nonce ใหม่"
    assert all(opens(client, k) for k in keys)


def test_a_run_cut_short_loses_nothing_and_the_next_run_finishes(secrets, client, monkeypatch):
    """ไฟดับ / process ถูกฆ่ากลางทาง หลังย้ายไปได้สองแถว

    ตัดด้วยการให้ตัวผนึกจริงล้มในแถวที่สาม — สองแถวแรกผนึกและ commit ด้วยของจริง
    """
    from app.core import keyvault

    keys = old_rows(client, secrets)
    secrets(B, previous=A)
    before = sealed_column(client)

    real_seal, calls = keyvault.seal, []

    def dies_on_the_third(plaintext: str) -> str:
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("power cut")
        return real_seal(plaintext)

    monkeypatch.setattr(keyvault, "seal", dies_on_the_third)
    with pytest.raises(RuntimeError, match="power cut"):
        reseal(client)
    monkeypatch.setattr(keyvault, "seal", real_seal)

    halfway = sealed_column(client)
    moved = [key_id for key_id in before if halfway[key_id] != before[key_id]]
    assert len(moved) == 2, "สองแถวแรกต้องอยู่ถาวรแล้ว ไม่ถูกม้วนกลับไปพร้อมแถวที่ล้ม"
    assert all(opens(client, k) for k in keys), "ครึ่งทาง: ทุกใบต้องยังเปิดได้"
    assert vault(client).json()["counts"] == {"current": 2, "previous": 3, "lost": 0, "off": 0}
    # สองแถวที่ย้ายไปแล้วต้องมีชื่อคนย้าย แม้รอบนั้นจะไม่จบ
    (actor, payload), = _audit_rows(client, "keyvault.reseal")
    assert actor and payload["resealed"] == 2 and payload["interrupted"] is True

    assert reseal(client).json()["resealed"] == 3, "รอบถัดไปทำแค่ส่วนที่เหลือ"
    secrets(B)
    assert all(opens(client, k) for k in keys), "เอาตัวเก่าออกหลังรอบที่สอง"


def test_two_runs_at_once_do_the_work_exactly_once(secrets, client):
    """ผู้ดูแลสองคนกดพร้อมกัน หรือกดปุ่มขณะที่ CLI รันอยู่ — ทั้งสองอ่านแถวเดียวกันว่ายังเก่า

    แต่ละแถวต้องถูกย้ายครั้งเดียว และค่าที่เหลืออยู่ในฐานต้องเป็นค่าที่เปิดได้
    """
    import asyncio

    from app.core import keyrotation
    from app.db.session import get_sessionmaker

    keys = old_rows(client, secrets)
    secrets(B, previous=A)

    async def run_one():
        async with get_sessionmaker()() as session:
            return await keyrotation.reseal(session)

    async def both():
        return await asyncio.gather(run_one(), run_one())

    first, second = client.portal.call(both)
    assert first.resealed + second.resealed == 5
    assert first.resealed + first.changed_meanwhile + first.already_current == 5
    secrets(B)
    assert all(opens(client, k) for k in keys)


# ── สิ่งที่ผนึกใหม่ต้องไม่ทำ ────────────────────────────────────────────────

def test_copies_that_open_under_neither_secret_are_left_exactly_as_they_were(secrets, client):
    """ใบที่เปิดไม่ได้ต้องไม่ถูกแตะ — วันหนึ่งอาจมีคนหา secret เดิมเจอ"""
    (lost,) = issued_under(client, secrets, C, 1, prefix="lost", fmt="new")
    old = issued_under(client, secrets, A, 2, prefix="old", fmt="v1")
    secrets(B, previous=A)
    stored = sealed_column(client)[lost["id"]]

    done = reseal(client).json()
    assert done["resealed"] == 2 and done["lost"] == 1
    assert sealed_column(client)[lost["id"]] == stored
    assert all(opens(client, k) for k in old)

    secrets(B, previous=C)          # หา secret เดิมเจอ → กู้ได้
    assert opens(client, lost)
    assert reseal(client).json()["resealed"] == 1


def test_values_already_under_the_current_secret_are_not_rewritten(secrets, client):
    """แถว `v1` ที่ secret ปัจจุบันเปิดได้อยู่แล้วต้องคงเดิม — รุ่นเก่ายังอ่านมันออกถ้าต้องถอย"""
    issued_under(client, secrets, A, 2, prefix="v1-", fmt="v1")
    before = sealed_column(client)

    done = reseal(client).json()
    assert done["resealed"] == 0 and done["already_current"] == 2
    assert sealed_column(client) == before


def test_it_refuses_to_run_with_the_feature_off_and_touches_nothing(secrets, client):
    """ไม่มี secret ปัจจุบัน = ไม่มีอะไรให้ผนึกใต้ — ต้องปฏิเสธ ไม่ใช่เขียนค่าว่างทับสำเนา"""
    old_rows(client, secrets)
    secrets(None, previous=A)
    before = sealed_column(client)

    done = reseal(client)
    assert done.status_code == 400
    assert "GW_KEY_REVEAL_SECRET" in done.json()["error"]["message"]
    assert sealed_column(client) == before
    assert all(value for value in before.values() if value), "ต้องไม่มีแถวไหนถูกล้างเป็นค่าว่าง"


def test_revoked_keys_are_resealed_too_so_the_count_reaches_zero(secrets, client):
    """ใบที่เพิกถอนแล้วเปิดดูไม่ได้อยู่แล้ว แต่สำเนายังอยู่ในฐาน

    ถ้าข้ามมันไป ตัวนับ "รอผนึกใหม่" จะไม่มีวันเป็นศูนย์ แล้วผู้ดูแลจะไม่รู้ว่าเอา previous
    ออกได้เมื่อไร · หลังเอาออกมันจะกลายเป็น "เปิดไม่ได้" ค้างในรายงานตลอดไป
    """
    keys = issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    client.delete(f"/admin/api-keys/{keys[0]['id']}", headers=auth(client.admin_key))
    secrets(B, previous=A)

    assert reseal(client).json()["resealed"] == 2
    secrets(B)
    assert vault(client).json()["counts"] == {"current": 2, "previous": 0, "lost": 0, "off": 0}
    assert reveal(client, keys[0]["id"]).status_code == 400, "เพิกถอนแล้วยังเปิดดูไม่ได้เหมือนเดิม"


# ── backup เก่ากับ secret ใหม่ ────────────────────────────────────────────────

@pytest.mark.sqlite_only
def test_an_older_backup_restored_after_the_change_needs_the_old_secret_back(
        secrets, temp_db, tmp_path):
    """สำรองฐาน → เปลี่ยน secret จนจบ (เอาตัวเก่าออกแล้ว) → restore ฐานที่สำรองไว้ทับ

    ฐานที่ได้กลับมาผนึกด้วย secret ตัวเก่า ซึ่งเครื่องนี้ไม่มีแล้ว — สำเนาทุกใบเปิดไม่ได้
    จนกว่าจะเอา secret เก่ากลับมาเป็น PREVIOUS · นี่คือเหตุที่ต้องเก็บ secret เก่าไว้ตราบที่
    ยังเก็บ backup ที่ทำก่อนการผนึกใหม่ (archive ของ scripts/backup.sh มี .env ตอนนั้นอยู่ข้างใน)
    """
    import sqlite3

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import _bootstrap_key
    from tests.keyvault_kit import issue_many

    backup = tmp_path / "backup.sqlite"
    secrets(A)
    with TestClient(create_app()) as gateway:
        gateway.admin_key = admin_key = _bootstrap_key(gateway)
        keys = issue_many(gateway, 3)
        # สำเนาที่สอดคล้องของฐานที่กำลังถูกใช้ — วิธีเดียวกับ `.backup` ใน scripts/backup.sh
        source, target = sqlite3.connect(temp_db), sqlite3.connect(backup)
        source.backup(target)
        source.close()
        target.close()

        secrets(B, previous=A)
        assert reseal(gateway).json()["resealed"] == 3
        secrets(B)
        assert all(opens(gateway, k) for k in keys), "เปลี่ยนจบแล้ว ทุกใบเปิดได้ด้วยตัวใหม่"

    # restore: เกตเวย์ปิดอยู่ วางไฟล์ที่สำรองไว้ทับ
    for leftover in (temp_db, temp_db.with_name(temp_db.name + "-wal"),
                     temp_db.with_name(temp_db.name + "-shm")):
        leftover.unlink(missing_ok=True)
    backup.rename(temp_db)

    with TestClient(create_app()) as gateway:
        gateway.admin_key = admin_key
        assert not any(opens(gateway, k) for k in keys), "ฐานเก่า + secret ใหม่ตัวเดียว"
        body = vault(gateway).json()
        assert body["counts"]["lost"] == 3 and "lost" in {w["code"] for w in body["warnings"]}

        secrets(B, previous=A)           # เอา secret เก่ากลับมา
        assert all(opens(gateway, k) for k in keys)
        assert reseal(gateway).json()["resealed"] == 3
        secrets(B)
        assert all(opens(gateway, k) for k in keys)


# ── ใครสั่งได้ และมีบันทึกว่าใครสั่ง ─────────────────────────────────────────

def test_only_an_admin_can_reseal(secrets, client, member_key):
    old_rows(client, secrets)
    secrets(B, previous=A)
    lecturer = person(client, "lecturer")
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    manager_key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                              json={"user_id": lecturer["id"], "name": "m"}).json()["api_key"]
    before = sealed_column(client)

    assert reseal(client, as_key=member_key).status_code in (401, 403)
    assert reseal(client, as_key=manager_key).status_code in (401, 403)
    assert sealed_column(client) == before


def _audit_rows(client, action: str):
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope

    async def read():
        async with session_scope() as session:
            rows = await session.execute(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.ts)
            )
            return [(row.actor_user_id, dict(row.payload or {})) for row in rows.scalars()]

    return client.portal.call(read)


def test_a_reseal_is_recorded_with_who_and_how_many_and_no_secret(secrets, client):
    """ท่าทีความปลอดภัยที่เปลี่ยนควรเป็นสิ่งที่มีคนทำ — และย้อนดูได้ว่าใคร เมื่อไร"""
    keys = old_rows(client, secrets)
    secrets(B, previous=A)
    assert vault(client).json()["last_reseal"] is None
    reseal(client)

    rows = _audit_rows(client, "keyvault.reseal")
    assert len(rows) == 1
    actor, payload = rows[0]
    assert actor, "ต้องมีชื่อคนทำ"
    assert payload["resealed"] == 5
    text = str(payload)
    assert A not in text and B not in text
    assert all(k["api_key"] not in text for k in keys)

    # บันทึกที่ไม่มีใครอ่านได้ไม่ใช่การบันทึก — หน้าสรุปบอกครั้งล่าสุดด้วย
    last = vault(client).json()["last_reseal"]
    assert last["by"] == actor and last["at"] and last["resealed"] == 5


# ── ไม่มีความลับใน log ตลอดทั้งเส้น ──────────────────────────────────────────

def test_nothing_secret_reaches_the_log_during_a_rotation(secrets, client, caplog):
    """เปิดดู · ดูสรุป · ผนึกใหม่ · เปิดใบที่หาย — log ทุกระดับต้องไม่มี secret หรือ key"""
    caplog.set_level(logging.DEBUG)
    (lost,) = issued_under(client, secrets, C, 1, prefix="lost", fmt="new")
    keys = old_rows(client, secrets)
    stored_before = sealed_column(client)
    secrets(B, previous=A)
    caplog.clear()

    listed(client)
    vault(client)
    assert opens(client, keys[0])
    assert reveal(client, lost["id"]).status_code == 400
    assert reseal(client).json()["resealed"] == 5
    stored_after = sealed_column(client)

    # ยืนยันว่ากำลังจับ log ระดับ DEBUG อยู่จริง ไม่ใช่ผ่านเพราะไม่มีอะไรถูกจับเลย
    logging.getLogger("app.tests.marker").debug("debug-capture-is-live")
    assert "debug-capture-is-live" in caplog.text
    for secret in (A, B, C):
        assert secret not in caplog.text
    for created in (lost, *keys):
        assert created["api_key"] not in caplog.text
        for stored in (stored_before[created["id"]], stored_after[created["id"]]):
            assert stored.rsplit(":", 1)[-1] not in caplog.text


# ── ทางบรรทัดคำสั่ง — ใช้ได้ตอนเกตเวย์ปิดอยู่ ─────────────────────────────────

def _cli(*args: str, current: str | None, previous: str | None = None, stdin: str = ""):
    # ตั้งเป็นค่าว่างแทนการไม่ตั้ง: ตัวแปรสภาพแวดล้อมชนะ `.env` เสมอ จึงไม่มีทางที่ `.env`
    # ของเครื่องที่รันเทสจะแอบเปิดฟีเจอร์ให้ process ลูก
    env = {**os.environ,
           "GW_KEY_REVEAL_SECRET": current or "",
           "GW_KEY_REVEAL_SECRET_PREVIOUS": previous or ""}
    return subprocess.run(
        [sys.executable, "-m", "app.tools", "keyvault", *args],
        cwd=REPO_ROOT, env=env, input=stdin, capture_output=True, text=True, timeout=120,
    )


def test_the_command_line_reports_and_reseals_against_the_same_database(secrets, client):
    """คำสั่งรันเป็นอีก process จริง ชี้ฐานเดียวกับเกตเวย์ที่เปิดอยู่"""
    (lost,) = issued_under(client, secrets, C, 1, prefix="lost", fmt="new")
    keys = old_rows(client, secrets)
    secrets(B, previous=A)

    status = _cli("status", current=B, previous=A)
    assert status.returncode == 1, "มีงานค้าง/มีใบหาย = exit ไม่เป็นศูนย์ ให้สคริปต์จับได้"
    assert "current=0" in status.stdout and "previous=5" in status.stdout
    assert "lost=1" in status.stdout
    assert lost["key_prefix"] in status.stdout, "บอกว่าใบไหน"

    done = _cli("reseal", "--yes", current=B, previous=A)
    assert done.returncode == 1, done.stderr       # ยังมีใบที่หายอยู่หนึ่งใบ
    assert "resealed 5" in done.stdout

    secrets(B)                                      # เกตเวย์ที่เปิดอยู่เห็นผลของคำสั่ง
    assert all(opens(client, k) for k in keys)

    for output in (status.stdout, status.stderr, done.stdout, done.stderr):
        for secret in (A, B, C):
            assert secret not in output
        assert all(k["api_key"] not in output for k in (lost, *keys))


def test_the_command_line_exits_zero_when_there_is_nothing_left_to_do(secrets, client):
    keys = old_rows(client, secrets)
    secrets(B, previous=A)

    assert _cli("reseal", "--yes", current=B, previous=A).returncode == 0
    again = _cli("reseal", current=B, previous=A)       # ไม่มีอะไรจะเขียน = ไม่ต้องถาม
    assert again.returncode == 0 and "resealed 0" in again.stdout
    assert _cli("status", current=B).returncode == 0
    secrets(B)
    assert all(opens(client, k) for k in keys)


def test_four_processes_resealing_at_once_move_every_row_exactly_once(secrets, client):
    """สี่ process จริงเริ่มพร้อมกันบนฐานเดียว — แบบเดียวกับ 4 worker ของเกตเวย์

    ไม่ใช่ทางที่ระบบรันเอง (การผนึกใหม่ไม่ทำตอนเริ่ม) แต่เป็นสิ่งที่เกิดได้: สองคนสั่งพร้อมกัน
    หรือสคริปต์ถูกเรียกซ้อน · ผลรวมต้องเท่าจำนวนแถวพอดี ไม่มีแถวไหนถูกย้ายสองครั้ง
    """
    import re

    keys = [*old_rows(client, secrets),
            *issued_under(client, secrets, A, 7, prefix="more-", fmt="v1")]
    secrets(B, previous=A)

    env = {**os.environ, "GW_KEY_REVEAL_SECRET": B, "GW_KEY_REVEAL_SECRET_PREVIOUS": A}
    running = [
        subprocess.Popen([sys.executable, "-m", "app.tools", "keyvault", "reseal", "--yes"],
                         cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    outputs = [proc.communicate(timeout=180) for proc in running]
    assert [proc.returncode for proc in running] == [0, 0, 0, 0], [err for _, err in outputs]

    moved = [int(re.search(r"resealed (\d+)", out).group(1)) for out, _ in outputs]
    assert sum(moved) == len(keys) == 12, moved
    secrets(B)
    assert all(opens(client, k) for k in keys)
    assert vault(client).json()["counts"] == {"current": 12, "previous": 0, "lost": 0, "off": 0}


def test_the_command_line_asks_before_it_moves_anything(secrets, client):
    """คำสั่งใช้ secret ของ shell ที่รัน ซึ่งอาจไม่ใช่ชุดเดียวกับของเกตเวย์

    จึงบอกก่อนว่าจะย้ายจาก secret ตัวไหนไปตัวไหน (เป็นป้าย เทียบกับคอนโซลได้) แล้วรอคำยืนยัน
    ไม่ยืนยัน = ไม่มีอะไรถูกเขียน
    """
    keys = old_rows(client, secrets)
    secrets(B, previous=A)
    before = sealed_column(client)
    ids = vault(client).json()

    refused = _cli("reseal", current=B, previous=A, stdin="no\n")
    assert refused.returncode == 2
    assert ids["current_key_id"] in refused.stdout and ids["previous_key_id"] in refused.stdout
    assert sealed_column(client) == before

    silent = _cli("reseal", current=B, previous=A, stdin="")       # ไม่มีใครตอบ (cron)
    assert silent.returncode == 2 and sealed_column(client) == before

    agreed = _cli("reseal", current=B, previous=A, stdin="reseal\n")
    assert agreed.returncode == 0, agreed.stderr
    assert "resealed 5" in agreed.stdout
    secrets(B)
    assert all(opens(client, k) for k in keys)


@pytest.mark.sqlite_only
def test_a_command_line_run_that_dies_midway_still_records_what_it_moved(secrets, client):
    """ผู้ตรวจอิสระ 2026-10-09: 4 ใบรอผนึก ล้มที่ใบที่สาม → สองใบย้ายไปแล้วถาวร แต่ไม่มีแถว
    keyvault.reseal ใน audit เลย เพราะ CLI จดหลังงานจบเท่านั้น

    ทาง route แก้เรื่องเดียวกันนี้ไปแล้ว (684b103) — ทางที่คนเข้าเครื่องได้ใช้ต้องไม่ใช่ทางที่ย้าย
    ของไปใต้ secret ตัวใหม่ได้โดยไม่ทิ้งร่องรอย · ทำให้การเขียนแถวที่สามล้มจริงที่ไดรเวอร์
    """
    from sqlalchemy import select, text

    from app.db.models import ApiKey
    from app.db.session import session_scope

    keys = issued_under(client, secrets, A, 4, prefix="k", fmt="new")
    secrets(B, previous=A)
    before = sealed_column(client)

    async def third_write_fails():
        async with session_scope() as session:
            order = (await session.execute(
                select(ApiKey.id).where(ApiKey.key_sealed != "")
                .order_by(ApiKey.created_at, ApiKey.id))).scalars().all()
            await session.execute(text(
                "CREATE TRIGGER third_fails BEFORE UPDATE OF key_sealed ON api_keys "
                f"WHEN OLD.id = '{order[2]}' "
                "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
            ))

    client.portal.call(third_write_fails)

    done = _cli("reseal", "--yes", current=B, previous=A)
    assert done.returncode == 3, done.stderr
    assert "Traceback" not in done.stderr, "บอกว่าเกิดอะไรเป็นประโยค ไม่ใช่โยน traceback ให้ผู้ดูแล"
    assert "simulated write failure" in done.stderr
    assert "2" in done.stderr and "again" in done.stderr.lower(), "ย้ายไปแล้วกี่ใบ และให้รันซ้ำ"

    after = sealed_column(client)
    assert sum(1 for key_id in before if after[key_id] != before[key_id]) == 2
    assert vault(client).json()["counts"] == {"current": 2, "previous": 2, "lost": 0, "off": 0}
    assert all(opens(client, k) for k in keys), "ครึ่งทาง: ทุกใบยังเปิดได้"

    (actor, payload), = _audit_rows(client, "keyvault.reseal")
    assert actor is None and payload["via"] == "cli"
    assert payload["resealed"] == 2 and payload["interrupted"] is True
    for value in filter(None, (*before.values(), *after.values())):
        assert value.rsplit(":", 1)[-1] not in str(payload)


def test_the_command_line_refuses_to_reseal_with_the_feature_off(secrets, client):
    old_rows(client, secrets)
    before = sealed_column(client)

    done = _cli("reseal", current=None, previous=A)
    assert done.returncode == 2
    assert "GW_KEY_REVEAL_SECRET" in done.stderr
    assert sealed_column(client) == before


def test_the_command_line_names_a_candidate_secret_without_echoing_it(secrets, client):
    """มี secret เก่าหลายตัวในที่เก็บรหัส ตัวไหนผนึกใบที่หาย? — เทียบป้ายได้โดยไม่ต้องลองทีละตัว"""
    issued_under(client, secrets, C, 1, prefix="lost", fmt="new")
    secrets(B)
    needed = vault(client).json()["lost"][0]["sealed_key_id"]

    right = _cli("key-id", current=B, stdin=C + "\n")
    wrong = _cli("key-id", current=B, stdin=A + "\n")
    assert right.returncode == 0 and right.stdout.strip() == needed
    assert wrong.stdout.strip() != needed
    assert C not in right.stdout + right.stderr
