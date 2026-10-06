"""sign in ต้องทำงานเท่ากัน ไม่ว่าชื่อผู้ใช้นั้นมีอยู่จริงหรือไม่

`/auth/login` ตอบข้อความเดียวกันทุกกรณีที่ล้มเหลว เพื่อไม่ให้ใครไล่ถามได้ว่าชื่อไหนมีอยู่
— แต่นาฬิกาตอบแทน (ตรวจ 2026-10-06 บน a5e9295): ชื่อที่มีจริง median 43.9 ms · ชื่อที่ไม่มี
1.2 ms เพราะชื่อที่ไม่มีอยู่ข้าม scrypt ไปทั้งก้อน · ต่างกันสามสิบกว่าเท่า ยิงทีละชื่อก็ได้
รายชื่อบัญชีทั้งระบบ

เทสในไฟล์นี้ **ไม่จับเวลา** — เทสที่จับเวลากระพริบบนเครื่องที่โหลดสูง และ "เร็วพอ ๆ กัน"
ก็ไม่ใช่สิ่งที่ต้องการอยู่แล้ว · สิ่งที่ต้องการคือ "ทำงานเดียวกัน" ซึ่งนับได้เด็ดขาด:
ทุกคำขอ sign in เรียก scrypt กี่ครั้ง ด้วยพารามิเตอร์อะไร
"""

from __future__ import annotations

import pytest

from app.core import passwords
from app.db.models import User
from app.db.session import session_scope

GOOD = "correct-horse-battery-staple"


@pytest.fixture
def scrypt_calls(monkeypatch):
    """ทุกครั้งที่ scrypt ถูกเรียก พร้อมพารามิเตอร์ที่กำหนดราคาของมัน"""
    calls: list[tuple[int, int, int, int]] = []
    real = passwords.hashlib.scrypt

    def counting(password, *, salt, n, r, p, dklen, maxmem=0):
        calls.append((n, r, p, dklen))
        return real(password, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=maxmem)

    monkeypatch.setattr(passwords.hashlib, "scrypt", counting)
    return calls


@pytest.fixture
def accounts(client):
    """บัญชีสามแบบที่คนนอกต้องแยกจากกันไม่ได้ (แบบที่สี่คือชื่อที่ไม่มีอยู่เลย)"""
    digest = passwords.hash_password(GOOD)

    async def create():
        async with session_scope() as session:
            session.add(User(external_id="has-password", role="member", password_hash=digest))
            # สมาชิกที่ใช้แต่ API key ไม่เคยตั้งรหัสผ่าน — เป็นบัญชีส่วนใหญ่ของระบบจริง
            session.add(User(external_id="key-only", role="member", password_hash=""))
            session.add(User(external_id="suspended", role="member", password_hash=digest,
                             status="suspended"))

    client.portal.call(create)
    # process ที่รับคำขอมาแล้ว — กรณี "คำขอแรกหลังเพิ่งขึ้น" มีเทสของตัวเองข้างล่าง
    passwords._dummy_hash()
    return client


def attempt(client, username, password="definitely-not-the-password"):
    return client.post("/auth/login", json={"username": username, "password": password})


WHO = ["has-password", "key-only", "suspended", "nobody-by-this-name"]


@pytest.mark.parametrize("username", WHO)
def test_every_failed_sign_in_costs_exactly_one_scrypt(accounts, scrypt_calls, username):
    response = attempt(accounts, username)

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Incorrect username or password."
    assert len(scrypt_calls) == 1, (
        f"{username!r}: scrypt ถูกเรียก {len(scrypt_calls)} ครั้ง — "
        "จำนวนครั้งที่ต่างกันคือเวลาที่ต่างกัน และเวลาที่ต่างกันบอกว่าชื่อไหนมีอยู่"
    )


def test_the_work_is_the_same_size_for_every_username(accounts, scrypt_calls):
    """นับครั้งเท่ากันยังไม่พอ — scrypt ที่ n เล็กกว่าก็คือเร็วกว่า"""
    cost = {}
    for username in WHO:
        scrypt_calls.clear()
        attempt(accounts, username)
        cost[username] = list(scrypt_calls)

    assert len({tuple(v) for v in cost.values()}) == 1, cost
    n, r, p, dklen = cost["nobody-by-this-name"][0]
    assert (n, r, p, dklen) == (passwords._SCRYPT_N, passwords._SCRYPT_R,
                                passwords._SCRYPT_P, passwords._KEY_LEN)


@pytest.mark.parametrize("first", WHO)
def test_the_first_sign_in_after_a_restart_does_not_give_it_away_either(
    accounts, scrypt_calls, monkeypatch, first
):
    """hash ตัวหลอกถูกสร้างครั้งแรกที่มีคน sign in — ราคานั้นต้องไม่ขึ้นกับชื่อที่พิมพ์"""
    monkeypatch.setattr(passwords, "_DUMMY_HASH", None)

    attempt(accounts, first)
    assert len(scrypt_calls) == 2, f"คำขอแรกด้วย {first!r}: {len(scrypt_calls)} ครั้ง"

    scrypt_calls.clear()
    attempt(accounts, "has-password")
    attempt(accounts, "nobody-by-this-name")
    assert len(scrypt_calls) == 2


def test_the_right_password_still_signs_in(accounts, scrypt_calls):
    response = attempt(accounts, "has-password", GOOD)
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "has-password"
    assert len(scrypt_calls) == 1


def test_no_password_on_the_account_means_no_way_in_whatever_is_typed(accounts):
    """hash ตัวหลอกต้องไม่กลายเป็นรหัสผ่านของบัญชีที่ไม่มีรหัสผ่าน"""
    for guess in ("", " ", GOOD, "definitely-not-the-password"):
        response = accounts.post("/auth/login", json={"username": "key-only", "password": guess})
        assert response.status_code in (400, 401), (guess, response.status_code)
    assert passwords.verify_login(GOOD, "") is False


def test_nothing_matches_the_dummy_and_nobody_knows_what_would(monkeypatch):
    """รหัสผ่านของตัวหลอกถูกสุ่มแล้วทิ้ง · สอง process ไม่ได้ตัวเดียวกัน"""
    monkeypatch.setattr(passwords, "_DUMMY_HASH", None)
    one = passwords._dummy_hash()
    assert passwords._dummy_hash() is one, "ต้องสร้างครั้งเดียวต่อ process"
    monkeypatch.setattr(passwords, "_DUMMY_HASH", None)
    assert passwords._dummy_hash() != one

    # ต่อให้รู้ hash ตัวหลอก การเทียบกับมันก็ไม่เคยทำให้เข้าได้
    assert passwords.verify_login("anything-at-all", "") is False


def test_a_suspended_account_with_the_right_password_is_told_it_is_suspended(accounts):
    """คำตอบนี้ได้เฉพาะคนที่รู้รหัสผ่านจริง จึงไม่ใช่ทางไล่ถามชื่อ"""
    response = attempt(accounts, "suspended", GOOD)
    assert response.status_code == 403
