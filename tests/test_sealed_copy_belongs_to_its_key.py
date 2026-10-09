"""สำเนาที่ผนึกไว้ต้องเป็นสำเนาของ key ใบที่มันเก็บอยู่ด้วย — เปิดออกอย่างเดียวไม่พอ

ผู้ตรวจอิสระ 2026-10-09: เอาค่า `key_sealed` ของใบ B ไปวางในแถวของใบ A (คนที่เขียนฐานได้ ·
merge ผิด · restore ผิดไฟล์) แล้ว Reveal ใบ A คืน **key ของ B** ภายใต้ prefix ของ A และ audit
จดว่ามีคนเปิดดู A · การผนึกใหม่ก็พาความผิดนั้นไปอยู่ใต้ secret ตัวใหม่โดยไม่รู้ตัว

แถวมี hash ของ key ตัวจริงอยู่แล้ว (`key_hash` — ตัวเดียวกับที่ใช้ยืนยันตัวทุกคำขอ) จึงตรวจได้
ว่าของที่เปิดออกมาเป็นของแถวนั้นจริง · HMAC หนึ่งครั้งต่อใบ วัดแล้ว 500 ใบไม่ถึง 1 ms จึงตรวจ
ทุกที่ที่เปิด: Reveal · รายการ key · หน้าสรุป · การผนึกใหม่
"""

from __future__ import annotations

import pytest

from tests.keyvault_kit import (
    A,
    B,
    auth,
    issued_under,
    listed,
    opens,
    put_sealed,
    reseal,
    reveal,
    sealed_column,
    secret_switch,
    vault,
)

FORMATS = pytest.mark.parametrize("fmt", ["v1", "new"])


@pytest.fixture
def secrets(monkeypatch):
    yield from secret_switch(monkeypatch)


def swapped(client, secrets, fmt="new"):
    """สามใบใต้ secret A แล้วเอาสำเนาของใบที่สองไปวางทับในแถวของใบแรก"""
    victim, source, bystander = issued_under(client, secrets, A, 3, prefix="k", fmt=fmt)
    put_sealed(client, victim["id"], sealed_column(client)[source["id"]])
    return victim, source, bystander


@FORMATS
def test_reveal_refuses_a_copy_that_belongs_to_another_key(secrets, client, fmt):
    victim, source, _ = swapped(client, secrets, fmt)

    response = reveal(client, victim["id"])
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["seal_state"] == "lost"
    assert error["details"]["reason"] == "not_this_key", "แยกจาก 'secret ผิดตัว' และ 'ข้อมูลเสีย' ได้"
    assert source["api_key"] not in response.text, "key ของอีกคนต้องไม่ออกไป"
    assert "GW_KEY_REVEAL_SECRET_PREVIOUS" not in error["message"], "secret ไหนก็ไม่ช่วย — อย่าชี้ไปทางนั้น"
    assert "new key" in error["message"].lower()

    history = client.get(f"/admin/api-keys/{victim['id']}/reveals",
                         headers=auth(client.admin_key)).json()["data"]
    assert history == [], "ไม่ได้ของ = ไม่มีบันทึกว่าเปิดดู"
    assert opens(client, source), "ใบที่สำเนาเป็นของมันเองยังเปิดได้ตามปกติ"


def test_the_list_and_the_summary_do_not_call_such_a_copy_revealable(secrets, client):
    victim, source, bystander = swapped(client, secrets)

    rows = listed(client)
    assert rows[victim["id"]]["seal_state"] == "lost"
    assert rows[victim["id"]]["revealable"] is False
    assert rows[source["id"]]["seal_state"] == rows[bystander["id"]]["seal_state"] == "current"

    body = vault(client).json()
    assert body["counts"] == {"current": 2, "previous": 0, "lost": 1, "off": 0}
    assert [(row["id"], row["reason"]) for row in body["lost"]] == [(victim["id"], "not_this_key")]
    codes = {w["code"] for w in body["warnings"]}
    assert "sealed_copy_mismatch" in codes, "แถวถูกแก้จากนอกเกตเวย์ — เป็นเรื่องที่ต้องมีคนรู้"
    assert "lost" not in codes, "ไม่ใช่เรื่องของ secret — อย่าบอกให้ไปตาม secret เก่า"
    assert source["api_key"] not in vault(client).text


@FORMATS
def test_a_reseal_does_not_carry_the_wrong_copy_forward(secrets, client, fmt):
    """ผนึกใหม่ต้องไม่ "ฟอก" สำเนาผิดใบให้กลายเป็นของที่ผนึกใต้ secret ตัวปัจจุบันอย่างถูกต้อง"""
    victim, source, bystander = swapped(client, secrets, fmt)
    secrets(B, previous=A)
    before = sealed_column(client)

    done = reseal(client).json()
    assert done["resealed"] == 2 and done["lost"] == 1
    assert sealed_column(client)[victim["id"]] == before[victim["id"]], "แถวนั้นต้องไม่ถูกแตะ"
    assert done["vault"]["counts"] == {"current": 2, "previous": 0, "lost": 1, "off": 0}
    assert "rotation_pending" not in {w["code"] for w in done["vault"]["warnings"]}

    assert reveal(client, victim["id"]).status_code == 400
    assert opens(client, source) and opens(client, bystander)


def test_the_key_itself_keeps_working_and_a_correct_copy_is_accepted_again(secrets, client):
    """สำเนาผิดใบกระทบแค่การเรียกดูซ้ำ · วางสำเนาที่ถูกกลับคืน (restore ที่ถูกไฟล์) แล้วเปิดได้"""
    victim, _source, _ = issued = swapped(client, secrets)
    assert client.get("/v1/models", headers=auth(victim["api_key"])).status_code == 200

    from app.core.keyvault import seal

    put_sealed(client, victim["id"], seal(victim["api_key"]))
    assert all(opens(client, created) for created in issued)
    assert vault(client).json()["warnings"] == []


def test_the_command_line_says_the_same_thing(secrets, client):
    """`keyvault status` เป็นอีก process — ต้องไม่บอกว่าใบนี้ "ผนึกด้วย secret ตัวอื่น" """
    import os
    import subprocess
    import sys

    from tests.conftest import REPO_ROOT

    victim, source, _ = swapped(client, secrets)
    done = subprocess.run(
        [sys.executable, "-m", "app.tools", "keyvault", "status"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        env={**os.environ, "GW_KEY_REVEAL_SECRET": A, "GW_KEY_REVEAL_SECRET_PREVIOUS": ""},
    )
    assert done.returncode == 1, done.stderr
    assert "current=2" in done.stdout and "lost=1" in done.stdout
    line = next(ln for ln in done.stdout.splitlines() if victim["key_prefix"] in ln)
    assert "copy of a different key" in line
    assert source["api_key"] not in done.stdout + done.stderr
