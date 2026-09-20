"""งานที่หนักพอต้องไม่รันบน event loop — และต้องพิสูจน์ได้ ไม่ใช่เชื่อเอา

เกตเวย์ตัวเดียวถือสตรีมที่ยังไหลอยู่หลายสิบเส้นพร้อมกัน ทุกเส้นอยู่บน event loop
เดียวกัน · งานที่บล็อก 40 ms หนึ่งครั้งจึงไม่ได้ทำให้คำขอเดียวช้าลง 40 ms แต่ทำให้
**ทุกสตรีมสะดุดพร้อมกัน** 40 ms โดยไม่มีอะไรใน log บอกว่าเกิดอะไรขึ้น

เทสในไฟล์นี้ไม่จับเวลา — เทสที่จับเวลาจะกระพริบบน CI ที่โหลดสูง · มันถามคำถามที่
ตรงกว่าและตอบได้เด็ดขาด: "โค้ดบรรทัดนี้กำลังรันอยู่บน thread ที่มี event loop ไหม"
`asyncio.get_running_loop()` โยน RuntimeError เมื่อถูกเรียกจาก thread ที่ไม่มี loop
ซึ่งคือสิ่งที่ `asyncio.to_thread` พาเราไป

สิ่งที่ *ไม่* อยู่ในไฟล์นี้ก็มีความหมายเท่ากัน · งานที่ถือ GIL ไว้ตลอด (base64 decode
ของรูป, parse YAML ด้วย libyaml) ย้ายไป thread แล้ว event loop ก็ยังไม่ได้รันอยู่ดี
ได้มาแต่ค่าข้าม thread — วัดแล้วเลยไม่ย้าย เหตุผลเต็มอยู่ในคอมเมนต์ที่ passwords.py
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.core import passwords

GOOD = "correct-horse-battery-staple"


def _off_the_loop() -> bool:
    """True เมื่อถูกเรียกจาก thread ที่ไม่มี event loop วิ่งอยู่"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def _spy(record: list[bool], real):
    def wrapper(*args, **kwargs):
        record.append(_off_the_loop())
        return real(*args, **kwargs)

    return wrapper


async def test_password_hashing_runs_off_the_event_loop(monkeypatch):
    """scrypt = ~41 ms บนเครื่องพัฒนา และตั้งใจให้ ~100 ms บนเซิร์ฟเวอร์เล็ก

    ราคานั้น *ถูกต้อง* สำหรับการ hash รหัสผ่าน — สิ่งที่ผิดคือการจ่ายมันบน event loop
    ย้ายแล้วได้ผลจริงเพราะ hashlib.scrypt ปล่อย GIL (วัดแล้ว: 4 ครั้งเรียงกัน 158 ms
    · 4 ครั้งพร้อมกันใน thread 53 ms)
    """
    where: list[bool] = []
    monkeypatch.setattr(passwords, "hash_password", _spy(where, passwords.hash_password))

    digest = await passwords.hash_password_async(GOOD)

    assert where == [True], "scrypt ยังรันอยู่บน event loop"
    assert passwords.verify_password(GOOD, digest)


async def test_password_verification_runs_off_the_event_loop(monkeypatch):
    stored = passwords.hash_password(GOOD)
    where: list[bool] = []
    monkeypatch.setattr(passwords, "verify_password", _spy(where, passwords.verify_password))

    assert await passwords.verify_password_async(GOOD, stored) is True
    assert await passwords.verify_password_async("wrong-but-long-enough", stored) is False
    assert where == [True, True]


async def test_a_too_short_password_never_reaches_scrypt(monkeypatch):
    """นโยบายความยาวตรวจได้ด้วย len() — ไม่ต้องจ่าย thread เพื่อไปโดนปฏิเสธข้างใน"""
    monkeypatch.setattr(
        passwords, "hash_password",
        lambda _pw: pytest.fail("ไม่ควรถึง scrypt เมื่อรหัสผ่านสั้นเกินนโยบาย"),
    )
    with pytest.raises(passwords.PasswordError):
        await passwords.hash_password_async("sht")


async def test_an_account_with_no_password_never_reaches_scrypt(monkeypatch):
    """บัญชีที่ยังไม่เคยตั้งรหัสผ่านต้อง sign in ไม่ได้ และต้องไม่เสีย thread ไปเปล่า ๆ"""
    monkeypatch.setattr(
        passwords, "verify_password",
        lambda *_a: pytest.fail("ไม่ควรถึง scrypt เมื่อบัญชีไม่มีรหัสผ่าน"),
    )
    assert await passwords.verify_password_async("anything", "") is False


def _make_user_with_a_password(client, username: str) -> None:
    """สมาชิกที่ตั้งรหัสผ่านไว้แล้ว — conftest ปิดทาง /auth/setup ไปแล้วเพราะมี admin อยู่"""
    import asyncio as _asyncio

    from app.db.models import User
    from app.db.session import session_scope

    async def create() -> None:
        async with session_scope() as session:
            session.add(User(
                external_id=username, display_name=username.title(),
                role="member", password_hash=passwords.hash_password(GOOD),
            ))

    _asyncio.run(create())


def test_the_login_endpoint_uses_the_off_loop_path(client, monkeypatch):
    """ของจริงทั้งเส้น: /auth/login ต้องไม่เรียก scrypt บน event loop

    เทสระดับ unit ข้างบนยังผ่านได้ถ้ามีใครเผลอเปลี่ยน endpoint กลับไปเรียกตัว sync
    ตรง ๆ — ข้อนี้จึงยิงผ่าน HTTP จริง

    เป็นเทสแบบ sync เพราะ TestClient รันแอปใน thread ของตัวเองพร้อม loop ของตัวเอง
    ถ้าเขียนเป็น async จะมี loop ของเทสซ้อนขึ้นมาอีกชั้นโดยไม่ได้ตรวจอะไรเพิ่ม
    """
    from app.core import passwords as pw_mod

    _make_user_with_a_password(client, "somchai")

    where: list[bool] = []
    monkeypatch.setattr(pw_mod, "verify_password", _spy(where, pw_mod.verify_password))

    signed_in = client.post("/auth/login", json={"username": "somchai", "password": GOOD})

    assert signed_in.status_code == 200, signed_in.text
    assert where == [True], "endpoint ยังเรียก verify_password บน event loop"


async def test_the_registry_watcher_reloads_off_the_event_loop(tmp_path, monkeypatch):
    """โหลดทะเบียนใหม่ = อ่านทุกไฟล์ + parse YAML + ตรวจด้วย pydantic

    วัดกับทะเบียน 63 โมเดล โดยจับช่วงที่ event loop ไม่ได้รันเลย: เรียกตรง ๆ 15.5 ms
    · ผ่าน to_thread 0.6 ms · เป็นงานเบื้องหลังที่ไม่มีใครรอคำตอบ จึงแลกเวลารวมกับ
    การไม่ทำให้สตรีมสะดุดได้เต็มที่
    """
    from app.registry.store import RegistryStore

    repo_config = Path(__file__).resolve().parent.parent / "config"
    target = tmp_path / "config"
    shutil.copytree(repo_config, target)

    store = RegistryStore(target, reload_seconds=0.01)
    where: list[bool] = []
    monkeypatch.setattr(store, "reload", _spy(where, store.reload))

    # `start()` โหลดครั้งแรกบน loop โดยตั้งใจ — ตอนนั้นยังไม่มีใครรับคำขอ และความผิด
    # ของทะเบียนควรโผล่ก่อนเกตเวย์เปิดรับงาน · ที่เทสนี้สนใจคือ *รอบตรวจ* หลังจากนั้น
    await store.start()
    assert where == [False], "start() ควรโหลดตรง ๆ บน loop"
    where.clear()

    # แตะไฟล์ให้ fingerprint เปลี่ยน แล้วปล่อยให้รอบตรวจหนึ่งรอบทำงาน
    model = next(iter((target / "models").glob("*.yaml")))
    model.write_text(model.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    try:
        for _ in range(300):
            if where:
                break
            await asyncio.sleep(0.01)
    finally:
        await store.stop()

    assert where, "รอบตรวจทะเบียนไม่ได้โหลดใหม่เลย"
    assert where[0] is True, "reload ยังรันอยู่บน event loop"
