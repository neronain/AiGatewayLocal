"""ผู้ดูแลต้องรู้ว่าสำเนาที่ผนึกไว้อยู่สถานะไหน ก่อนที่จะมีคนกด Reveal แล้วล้ม

การเปลี่ยน secret มีหลายจังหวะที่พลาดได้โดยไม่มีอะไรเตือน: ตั้งสองตัวแปรเป็นค่าเดียวกัน ·
ลืมเอาตัวเก่าออกทั้งที่ไม่มีอะไรใช้แล้ว · เอาตัวหลักออกทั้งที่ยังมีสำเนาอยู่ · ใส่ตัวเก่าผิดตัว
ทุกอย่างนี้เดิมจบที่อาการเดียวกันคือ "กด Reveal แล้วไม่ได้" โดยไม่บอกว่าใบไหนและเพราะอะไร

ไฟล์นี้ดูว่าระบบบอกเอง: หน้าสรุปของผู้ดูแล · บรรทัดนับตอนเริ่ม process · และคอนโซล
— และไม่มีที่ไหนในนั้นพิมพ์ secret หรือตัว key ออกมา
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.keyvault_kit import (
    A,
    B,
    C,
    auth,
    issue_many,
    issued_under,
    listed,
    person,
    sealed_column,
    secret_switch,
    vault,
)


@pytest.fixture
def secrets(monkeypatch):
    yield from secret_switch(monkeypatch)


def codes(body) -> set[str]:
    return {warning["code"] for warning in body["warnings"]}


def mixed(client, secrets, fmt="new"):
    """หนึ่งใบที่หาย · สองใบที่รอผนึกใหม่ · หนึ่งใบใต้ตัวปัจจุบัน แล้วจบที่ current=B previous=A"""
    (lost,) = issued_under(client, secrets, C, 1, prefix="lost", fmt=fmt)
    pending = issued_under(client, secrets, A, 2, prefix="pending", fmt=fmt)
    (fresh,) = issued_under(client, secrets, B, 1, prefix="fresh", fmt=fmt)
    secrets(B, previous=A)
    return lost, pending, fresh


# ── หน้าสรุปของผู้ดูแล ──────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", ["v1", "new"])
def test_the_summary_counts_each_state_and_names_what_is_lost(secrets, client, fmt):
    lost, _pending, _fresh = mixed(client, secrets, fmt)

    body = vault(client).json()
    assert body["enabled"] is True
    assert body["counts"] == {"current": 1, "previous": 2, "lost": 1, "off": 0}
    assert body["sealed"] == 4
    # "ใบไหน": ชี้ตัวได้จากสิ่งที่ผู้ดูแลเห็นบนจออยู่แล้ว ไม่ใช่แค่จำนวน
    assert [row["id"] for row in body["lost"]] == [lost["id"]]
    assert body["lost"][0]["key_prefix"] == lost["key_prefix"]
    assert body["lost"][0]["name"] == "lost0"
    assert body["lost"][0]["revoked"] is False
    assert {"rotation_pending", "lost"} <= codes(body)


def test_the_summary_says_which_secret_a_lost_copy_needs(secrets, client):
    """ป้ายของ secret ที่ผนึกใบนั้น — เอาไปเทียบกับ secret เก่าที่เก็บไว้ได้โดยไม่ต้องลองทีละตัว"""
    lost, _pending, _fresh = mixed(client, secrets)
    body = vault(client).json()
    needed = body["lost"][0]["sealed_key_id"]

    assert body["lost"][0]["reason"] == "unknown_secret"
    assert needed not in {body["current_key_id"], body["previous_key_id"]}
    assert body["current_key_id"] != body["previous_key_id"]
    secrets(C)                      # secret ที่ผนึกใบนั้นจริง ต้องได้ป้ายเดียวกัน
    assert vault(client).json()["current_key_id"] == needed


def test_the_summary_carries_no_secret_and_no_key(secrets, client):
    lost, pending, fresh = mixed(client, secrets)
    stored = sealed_column(client)

    response = vault(client)
    assert response.status_code == 200, "คำตอบ 404 ก็ไม่มีความลับ — ต้องเป็นคำตอบจริง"
    text = response.text
    assert lost["key_prefix"] in text, "และต้องเป็นคำตอบที่พูดถึงใบพวกนี้จริง"
    for secret in (A, B, C):
        assert secret not in text
    for created in (lost, *pending, fresh):
        assert created["api_key"] not in text
        assert stored[created["id"]] not in text
        assert stored[created["id"]].rsplit(":", 1)[-1] not in text


def test_only_an_admin_reads_the_summary(secrets, client, member_key):
    """เหมือน Reveal: manager ดูแลคนได้ แต่เรื่องของ secret ที่ผนึกเป็นของผู้ดูแลระบบ"""
    secrets(A)
    lecturer = person(client, "lecturer")
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    manager_key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                              json={"user_id": lecturer["id"], "name": "m"}).json()["api_key"]

    assert vault(client).status_code == 200
    assert vault(client, as_key=manager_key).status_code in (401, 403)
    assert vault(client, as_key=member_key).status_code in (401, 403)


def test_a_healthy_setup_has_nothing_to_warn_about(secrets, client):
    """ยามที่ร้องตลอดคือยามที่ไม่มีใครฟัง"""
    issued_under(client, secrets, A, 2, prefix="k", fmt="v1")
    issued_under(client, secrets, A, 1, prefix="n", fmt="new")

    body = vault(client).json()
    assert body["warnings"] == []
    assert body["counts"] == {"current": 3, "previous": 0, "lost": 0, "off": 0}
    assert body["lost"] == []


def test_a_gateway_that_never_used_the_feature_reports_it_off_and_quiet(secrets, client):
    secrets()
    issue_many(client, 1)

    body = vault(client).json()
    assert body["enabled"] is False
    assert body["sealed"] == 0
    assert body["warnings"] == []
    assert body["current_key_id"] is None and body["previous_key_id"] is None


# ── จังหวะที่พลาดได้ ────────────────────────────────────────────────────────

def test_the_same_value_in_both_variables_is_called_out(secrets, client):
    """คัดลอกบรรทัดแล้วลืมแก้ค่า — ดูเหมือนตั้งการเปลี่ยนผ่านไว้แล้ว แต่ไม่มีอะไรเกิดขึ้น"""
    issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    secrets(A, previous=A)

    body = vault(client).json()
    assert "previous_equals_current" in codes(body)
    assert body["previous_key_id"] is None, "ค่าเดียวกัน = ไม่มีตัวเก่าจริง"
    assert body["counts"]["current"] == 2 and body["counts"]["previous"] == 0
    assert "rotation_pending" not in codes(body)


def test_a_previous_secret_nothing_needs_any_more_can_be_removed(secrets, client):
    """ผนึกใหม่ครบแล้วแต่ตัวเก่ายังค้างใน .env — secret ที่ไม่ต้องใช้แล้วไม่ควรนอนอยู่บนเครื่อง"""
    issued_under(client, secrets, B, 2, prefix="k", fmt="new")
    secrets(B, previous=A)

    body = vault(client).json()
    assert "previous_unused" in codes(body)
    message = next(w["message"] for w in body["warnings"] if w["code"] == "previous_unused")
    assert "GW_KEY_REVEAL_SECRET_PREVIOUS" in message and "remove" in message.lower()


def test_while_copies_still_need_the_previous_secret_it_is_not_called_removable(secrets, client):
    issued_under(client, secrets, A, 1, prefix="k", fmt="v1")
    secrets(B, previous=A)

    body = vault(client).json()
    assert "previous_unused" not in codes(body)
    assert "rotation_pending" in codes(body)


def test_switching_the_feature_off_with_copies_still_stored_is_called_out(secrets, client):
    """เอา secret หลักออกไม่ได้ลบสำเนา — ของที่ผนึกไว้ยังนอนอยู่ในฐาน และควรมีคนรู้"""
    issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    secrets()

    body = vault(client).json()
    assert body["enabled"] is False
    assert body["counts"] == {"current": 0, "previous": 0, "lost": 0, "off": 2}
    assert "sealed_but_disabled" in codes(body)


def test_a_previous_secret_without_a_current_one_is_called_out(secrets, client):
    """ย้ายค่าไปไว้ผิดตัวแปร: ตัวเก่ามี ตัวหลักไม่มี — ฟีเจอร์ปิด และควรบอกว่าทำไม"""
    issued_under(client, secrets, A, 1, prefix="k", fmt="new")
    secrets(None, previous=A)

    body = vault(client).json()
    assert body["enabled"] is False
    assert "previous_without_current" in codes(body)


def test_a_previous_secret_that_opens_nothing_lost_is_not_called_removable(secrets, client):
    """ใส่ตัวเก่าผิดตัว: มีใบที่เปิดไม่ได้ และ previous ที่ตั้งไว้ก็ไม่ใช่ตัวที่ผนึกมัน

    บอกว่า "เอา previous ออกได้" ในจังหวะนี้คือบอกให้เลิกหาทั้งที่ยังหาไม่เจอ
    """
    issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    secrets(B, previous=C)

    body = vault(client).json()
    assert body["counts"]["lost"] == 2
    assert "previous_does_not_match" in codes(body)
    assert "previous_unused" not in codes(body)


# ── บรรทัดนับตอนเริ่ม process ───────────────────────────────────────────────

def _first_run(secrets):
    """process แรก: สร้างฐานที่มีครบสามสถานะ แล้วปิด · คืนของที่ออกไปและสิ่งที่อยู่ในฐาน"""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import _bootstrap_key

    secrets(A)
    with TestClient(create_app()) as first:
        first.admin_key = _bootstrap_key(first)
        lost, pending, fresh = mixed(first, secrets, "v1")
        more = issued_under(first, secrets, A, 1, prefix="more", fmt="new")
        return [lost, *pending, fresh, *more], sealed_column(first)


def test_startup_logs_a_count_of_each_state_and_changes_nothing(secrets, temp_db, caplog):
    """เริ่ม process แล้ว log ต้องบอกจำนวนของแต่ละสถานะ — และ **ไม่แตะ** ค่าที่ผนึกไว้

    เกตเวย์จริงรัน 4 worker ที่เริ่มพร้อมกัน อะไรที่ทำ "ตอนเริ่ม" จึงรันสี่ครั้งซ้อนกัน
    ตอนเริ่มจึงอ่านและรายงานอย่างเดียว การผนึกใหม่เป็นสิ่งที่ผู้ดูแลสั่งเอง
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    issued, before = _first_run(secrets)
    secrets(B, previous=A)
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app()) as second:
        after = sealed_column(second)

    lines = [r.getMessage() for r in caplog.records if "sealed" in r.getMessage()]
    counted = next(line for line in lines if "current=" in line)
    assert "current=1" in counted and "previous=3" in counted and "lost=1" in counted
    assert after == before, "ตอนเริ่มต้องไม่เขียนทับค่าที่ผนึกไว้"

    # ไม่มี secret · ไม่มีตัว key · ไม่มีค่าที่ผนึก · ไม่มีแม้แต่ส่วนต้นของ key อยู่ใน log
    for secret in (A, B, C):
        assert secret not in caplog.text
    for created in issued:
        assert created["api_key"] not in caplog.text
        assert created["key_prefix"] not in caplog.text
        assert before[created["id"]].rsplit(":", 1)[-1] not in caplog.text


def test_startup_warns_about_copies_that_no_longer_open(secrets, temp_db, caplog):
    """ผู้ดูแลต้องรู้จาก log ตอนเริ่ม ไม่ใช่จากคนที่มาบอกว่ากด Reveal แล้วไม่ได้"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    _first_run(secrets)
    secrets(B)                       # เอาตัวเก่าออกทั้งที่ยังไม่ได้ผนึกใหม่
    caplog.clear()
    caplog.set_level(logging.INFO)
    with TestClient(create_app()):
        pass

    alarms = [r for r in caplog.records
              if r.levelno >= logging.WARNING and "sealed" in r.getMessage()]
    assert alarms, "ต้องมีคำเตือนตอนเริ่ม"
    said = " ".join(r.getMessage() for r in alarms)
    assert "GW_KEY_REVEAL_SECRET_PREVIOUS" in said and "new key" in said.lower()


def test_startup_is_quiet_when_nothing_is_wrong(secrets, temp_db, caplog):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import _bootstrap_key

    secrets(A)
    with TestClient(create_app()) as first:
        first.admin_key = _bootstrap_key(first)
        issue_many(first, 2)
    caplog.clear()
    caplog.set_level(logging.INFO)
    with TestClient(create_app()):
        pass

    about = [r for r in caplog.records if "sealed" in r.getMessage()]
    assert about and all(r.levelno < logging.WARNING for r in about)
    assert any("current=2" in r.getMessage() for r in about)


def test_a_failing_check_does_not_stop_the_gateway_from_starting(secrets, temp_db, monkeypatch):
    """บรรทัดรายงานเป็นของประกอบ · ฐานที่ถามไม่ได้ชั่วครู่ต้องไม่ทำให้เกตเวย์ไม่ขึ้น"""
    from fastapi.testclient import TestClient

    from app.core import keyrotation
    from app.main import create_app

    async def broken(_session):
        raise RuntimeError("database went away")

    secrets(A)
    monkeypatch.setattr(keyrotation, "survey", broken)
    with TestClient(create_app()) as up:
        assert up.get("/healthz").status_code == 200


# ── คอนโซล: รันตัววาดของจริงใน node ด้วยข้อมูลที่ API คืนมาจริง ───────────────

def _console(functions: list[str], expression: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(
        encoding="utf-8")

    def function(name: str) -> str:
        start = source.index(f"function {name}(")
        return source[start:re.compile(r"^}\n", re.M).search(source, start).end()]

    script = (
        "const esc = (v) => String(v ?? '').replace(/[&<>\"]/g, "
        "(c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\n"
        "const num = (v) => String(v || 0);\n"
        + "\n".join(function(name) for name in functions)
        + f"\nconsole.log(JSON.stringify({expression}));\n"
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_console_marks_each_key_with_its_state(secrets, client):
    lost, pending, fresh = mixed(client, secrets)
    rows = listed(client)
    picked = [rows[fresh["id"]], rows[pending[0]["id"]], rows[lost["id"]]]

    pills = _console(["sealPill"], f"{json.dumps(picked)}.map(sealPill)")
    assert pills[0] == "", "ใบที่ปกติไม่ต้องมีป้ายอะไร"
    assert "pill" in pills[1] and "pill" in pills[2] and pills[1] != pills[2]

    notes = _console(["sealNote"], f"{json.dumps(picked)}.map(sealNote)")
    assert notes[0] == "" and notes[1] == ""
    assert "GW_KEY_REVEAL_SECRET_PREVIOUS" in notes[2], "บอกทางกู้"
    assert "key ใหม่" in notes[2], "บอกทางออกเมื่อกู้ไม่ได้"


def test_the_console_panel_shows_counts_what_is_lost_and_offers_the_reseal(secrets, client):
    lost, _pending, _fresh = mixed(client, secrets)

    html = _console(["vaultAdvice", "keyVaultPanel"],
                    f"keyVaultPanel({json.dumps(vault(client).json())})")
    assert 'id="vault-reseal"' in html, "มีใบที่รอผนึกใหม่ = ต้องมีปุ่ม"
    assert "Re-seal 2 key" in html
    assert lost["key_prefix"] in html, "บอกว่าใบไหนเปิดไม่ได้"
    assert lost["api_key"] not in html


def test_the_console_panel_stays_out_of_the_way_when_all_is_well(secrets, client):
    """แผงที่ขึ้นตลอดคือแผงที่คนเลิกอ่าน — ไม่มีอะไรต้องทำก็ไม่ต้องมีอะไรขึ้น"""
    issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    assert _console(["vaultAdvice", "keyVaultPanel"],
                    f"keyVaultPanel({json.dumps(vault(client).json())})") == ""


def test_the_console_does_not_offer_a_reseal_with_nothing_to_reseal(secrets, client):
    issued_under(client, secrets, A, 2, prefix="k", fmt="new")
    secrets(B)                                      # สองใบหาย ไม่มีใบไหนรอผนึก

    html = _console(["vaultAdvice", "keyVaultPanel"],
                    f"keyVaultPanel({json.dumps(vault(client).json())})")
    assert html and 'id="vault-reseal"' not in html
