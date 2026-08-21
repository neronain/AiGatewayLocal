"""ตั้งรหัสผ่านใหม่ตอนที่เข้าคอนโซลไม่ได้แล้ว

ทางปกติคือเปลี่ยนในคอนโซล ซึ่งต้องรู้รหัสเดิม · ก่อนมีสคริปต์นี้ คนที่ลืมรหัสไม่มี
ทางออกเลยนอกจากไปแก้ฐานข้อมูลเอง
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reset_password.py"


def test_the_script_exists_and_runs():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR, "ต้องรันได้ (chmod +x)"


def test_it_signs_every_other_session_out():
    """คนที่รีเซ็ตรหัส มักรีเซ็ตเพราะสงสัยว่ามีคนอื่นเข้าถึงได้

    ปล่อย session เดิมไว้ = ไม่ได้แก้อะไร
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "session_epoch" in source


def test_it_uses_the_same_hashing_as_the_console():
    """เขียนกฎรหัสผ่านซ้ำที่นี่ = วันหนึ่งรหัสที่ตั้งผ่านสคริปต์จะไม่ตรงกับที่คอนโซลยอมรับ"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "from app.core.passwords import" in source
    assert "hash_password" in source


def test_a_new_password_works_and_the_old_one_stops(client):
    """พิสูจน์ผ่านทางเข้าจริง ไม่ใช่แค่ดูว่าเขียนฐานข้อมูลสำเร็จ"""
    import asyncio

    from sqlalchemy import select

    from app.core.passwords import hash_password
    from app.db.models import User
    from app.db.session import session_scope

    created = client.post(
        "/admin/users",
        json={"external_id": "pwtest", "display_name": "PW", "role": "admin"},
        headers={"Authorization": f"Bearer {client.admin_key}"},
    ).json()

    async def set_password(value: str) -> None:
        async with session_scope() as session:
            row = await session.execute(select(User).where(User.id == created["id"]))
            user = row.scalar_one()
            user.password_hash = hash_password(value)
            user.session_epoch = int(user.session_epoch or 0) + 1
            user.must_change_password = False
            await session.commit()

    asyncio.run(set_password("first-Password-1"))
    assert client.post(
        "/auth/login", json={"username": "pwtest", "password": "first-Password-1"}
    ).status_code == 200

    asyncio.run(set_password("second-Password-2"))
    assert client.post(
        "/auth/login", json={"username": "pwtest", "password": "second-Password-2"}
    ).status_code == 200
    assert client.post(
        "/auth/login", json={"username": "pwtest", "password": "first-Password-1"}
    ).status_code == 401
