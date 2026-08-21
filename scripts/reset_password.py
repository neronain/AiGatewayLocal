#!/usr/bin/env python3
"""ตั้งรหัสผ่านคอนโซลใหม่ให้บัญชีหนึ่ง — สำหรับตอนที่เข้าไม่ได้แล้ว

    python scripts/reset_password.py admin                  # สุ่มให้ พิมพ์ออกมาครั้งเดียว
    python scripts/reset_password.py admin --password '…'   # ตั้งเอง
    python scripts/reset_password.py --list                 # ดูว่ามีบัญชีอะไรบ้าง

ทางปกติของการเปลี่ยนรหัสคือทำในคอนโซล (My account → Change password) ซึ่งต้อง
รู้รหัสเดิม · สคริปต์นี้มีไว้สำหรับกรณีที่ *ไม่รู้รหัสเดิมแล้ว* ซึ่งก่อนหน้านี้ไม่มีทางออก
เลยนอกจากไปแก้ฐานข้อมูลเอง

รันบนเครื่องที่ติดตั้งเกตเวย์ไว้ เพราะมันอ่าน .env ตัวเดียวกับที่ service ใช้ —
ชี้ผิดฐานข้อมูลแล้วจะดูเหมือนสำเร็จแต่เข้าไม่ได้อยู่ดี

ทุกครั้งที่ตั้งใหม่ **session เดิมทุกอันถูกเตะออก** (เลื่อน session_epoch) เพราะคน
ที่ต้องรีเซ็ตรหัสมักรีเซ็ตเพราะสงสัยว่ามีคนอื่นเข้าถึงได้ — ปล่อย session เก่าไว้ก็
เท่ากับไม่ได้แก้อะไร
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.passwords import PasswordError, hash_password  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402


async def list_accounts() -> int:
    await init_db()
    async with session_scope() as session:
        users = (await session.execute(select(User).order_by(User.role, User.external_id))).scalars()
        rows = [u for u in users]
    if not rows:
        print("ไม่มีบัญชีในฐานข้อมูลนี้ — ชี้ถูก GW_DATABASE_URL หรือเปล่า")
        return 1
    print(f"{'ID':<22} {'ROLE':<9} {'มีรหัสผ่าน':<12} ชื่อ")
    for u in rows:
        has = "มี" if u.password_hash else "ยังไม่มี"
        print(f"{u.external_id:<22} {u.role:<9} {has:<12} {u.display_name or ''}")
    return 0


async def reset(external_id: str, password: str | None) -> int:
    await init_db()
    chosen = password or secrets.token_urlsafe(12)

    async with session_scope() as session:
        result = await session.execute(select(User).where(User.external_id == external_id))
        user = result.scalar_one_or_none()
        if user is None:
            print(f"ไม่มีบัญชีชื่อ '{external_id}' — ดูรายชื่อ: --list", file=sys.stderr)
            return 1
        try:
            user.password_hash = hash_password(chosen)
        except PasswordError as exc:
            # กฎความยาว/ความซับซ้อนเป็นของ hash_password ที่เดียว — ไม่เขียนซ้ำที่นี่
            # ให้ผิดกันคนละแบบกับตอนเปลี่ยนผ่านคอนโซล
            print(f"รหัสผ่านใช้ไม่ได้: {exc}", file=sys.stderr)
            return 1
        # เตะ session เดิมออกทั้งหมด · ดูเหตุผลในหัวไฟล์
        user.session_epoch = int(user.session_epoch or 0) + 1
        # ตั้งเองแล้วไม่ต้องบังคับเปลี่ยนอีกรอบ — คนที่รันคำสั่งนี้คือผู้ดูแลเครื่อง
        user.must_change_password = False
        await session.commit()

    print("=" * 68)
    print(f"  ตั้งรหัสผ่านใหม่ให้ {external_id} แล้ว")
    print(f"  username: {external_id}")
    print(f"  password: {chosen}")
    print("  session เดิมทุกอันถูกเตะออก — ต้องเข้าใหม่ทุกเครื่อง")
    print("=" * 68)
    if not password:
        print("  รหัสนี้สุ่มมา แสดงครั้งเดียว · เก็บไว้เดี๋ยวนี้")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ตั้งรหัสผ่านคอนโซลใหม่ (ใช้ตอนลืมรหัสเดิม)",
    )
    parser.add_argument("user", nargs="?", help="external_id ของบัญชี เช่น admin")
    parser.add_argument("--password", help="ตั้งเอง · ไม่ใส่ = สุ่มให้")
    parser.add_argument("--list", action="store_true", help="แสดงบัญชีทั้งหมด")
    args = parser.parse_args()

    if args.list:
        return asyncio.run(list_accounts())
    if not args.user:
        parser.error("ต้องบอกว่าจะตั้งให้บัญชีไหน — หรือใช้ --list ดูก่อน")
    return asyncio.run(reset(args.user, args.password))


if __name__ == "__main__":
    raise SystemExit(main())
