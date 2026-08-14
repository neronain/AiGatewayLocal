"""Keeping an issued API key readable, on purpose and at a stated cost.

A key is normally stored as a SHA-256 digest and nothing else, which is why
"show me that key again" has no answer: there is nothing to show. That is the
right default and it stays the default.

But an operator running a small gateway hits the other side of it. Somebody
loses the key they were given, and the only remedy is a new one — which means
finding every config file, CI secret and laptop that held the old one. For a
class of thirty that is a morning's work caused by one person's mislaid note.

So a second copy can be kept, sealed with AES-GCM under a secret that lives
**outside the database**, and an administrator can open it. The trade is real
and worth stating plainly:

  - a leaked database dump, on its own, still reveals nothing
  - a host compromise that reaches both the dump and the environment reveals
    every sealed key at once

Which is why this is off unless `GW_KEY_REVEAL_SECRET` is set. Turning on a
weaker posture should be something somebody did, not something that happened.

The secret must not be stored in the same database as the sealed values — that
would put the lock and its key in one box and buy nothing at all.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

# ป้ายรุ่นของรูปแบบ — ขึ้นต้นด้วยตัวนี้เสมอ จะได้เปลี่ยนวิธีผนึกในอนาคตโดยยัง
# อ่านของเก่าออก แทนที่จะเดาจากความยาวแล้วพังตอนอ่านผิดรุ่น
FORMAT = "v1"
NONCE_BYTES = 12


def _cipher() -> AESGCM | None:
    """คืน cipher เมื่อผู้ดูแลเปิดใช้ไว้ · None = ปิดอยู่ ซึ่งเป็นค่าตั้งต้น"""
    secret = (get_settings().key_reveal_secret or "").strip()
    if not secret:
        return None
    # ยืดรหัสผ่านที่คนพิมพ์เองให้เป็นคีย์ 256 บิต · salt คงที่ได้เพราะ secret นี้
    # ไม่ใช่รหัสผ่านของผู้ใช้และไม่ถูกนำไปเทียบกับฐานข้อมูลรหัสผ่านที่ไหน
    material = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"litegate-key-reveal", 200_000)
    return AESGCM(material)


def reveal_enabled() -> bool:
    """เปิดให้เรียกดู key เดิมได้ไหม — หน้าเว็บถามก่อนวาดปุ่ม"""
    return _cipher() is not None


def seal(plaintext: str) -> str:
    """ปิดผนึกไว้อ่านทีหลัง · คืนค่าว่างเมื่อปิดใช้อยู่ = ไม่เก็บอะไรเลย"""
    cipher = _cipher()
    if cipher is None or not plaintext:
        return ""
    nonce = os.urandom(NONCE_BYTES)
    blob = nonce + cipher.encrypt(nonce, plaintext.encode(), None)
    return f"{FORMAT}:{base64.urlsafe_b64encode(blob).decode()}"


def unseal(sealed: str) -> str | None:
    """เปิดผนึก · None เมื่อเปิดไม่ได้ ไม่ว่าด้วยเหตุใด

    เหตุที่เปิดไม่ได้มีหลายแบบและปลายทางต้องแยกออก จึงไม่โยน exception ที่ต่างกัน
    ให้ไปไล่จับ: ไม่มีของผนึกไว้ (key เก่าที่ออกก่อนเปิดฟีเจอร์), ฟีเจอร์ถูกปิดอยู่,
    หรือ secret ถูกเปลี่ยนไปแล้ว — ทั้งสามอย่างจบที่ "บอกผู้ใช้ว่าดูไม่ได้" เหมือนกัน
    """
    cipher = _cipher()
    if cipher is None or not sealed:
        return None
    version, _, payload = sealed.partition(":")
    if version != FORMAT or not payload:
        return None
    try:
        blob = base64.urlsafe_b64decode(payload.encode())
        return cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None).decode()
    except (InvalidTag, ValueError):
        # secret เปลี่ยน หรือข้อมูลเสีย — ทั้งคู่แปลว่าอ่านไม่ได้ ไม่ใช่ว่าระบบพัง
        return None
