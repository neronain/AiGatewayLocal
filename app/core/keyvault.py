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

Changing the secret
-------------------
A secret that cannot be changed is a secret that cannot be recovered from once
it leaks. So there are two: `GW_KEY_REVEAL_SECRET` seals everything new, and
`GW_KEY_REVEAL_SECRET_PREVIOUS` is tried second when opening, for as long as
something is still sealed under the old one. Moving those values over is a
separate, deliberate step (`app/core/keyrotation.py`) — nothing here writes.

The previous secret never switches the feature on by itself, and nothing is
ever sealed under it: it exists to get out of a secret, not to keep using one.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

# ป้ายรุ่นของรูปแบบ — ขึ้นต้นด้วยตัวนี้เสมอ จะได้เปลี่ยนวิธีผนึกในอนาคตโดยยัง
# อ่านของเก่าออก แทนที่จะเดาจากความยาวแล้วพังตอนอ่านผิดรุ่น
#
#   v1:<b64(nonce + ciphertext)>            รุ่น ≤ 1.12.1 — ไม่บอกว่า secret ไหนผนึก
#   v2:<key id>:<b64(nonce + ciphertext)>   จดป้ายของ secret ที่ผนึกไว้ข้างหน้า
#
# ทำไมต้องมี v2 (2026-10-09): พอมี secret ได้สองตัว คำถาม "ใบนี้เปิดไม่ได้เพราะอะไร"
# ตอบด้วย v1 ไม่ได้เลย — ลองเปิดแล้วล้ม บอกได้แค่ว่าล้ม ไม่รู้ว่า secret ผิดตัวหรือข้อมูลเสีย
# และไม่รู้ว่าต้องไปตาม secret ตัวไหนกลับมา · v2 จดป้ายสั้น ๆ ของ secret ไว้ จึงแยกสอง
# เหตุนี้ออก และผู้ดูแลเอาป้ายไปเทียบกับ secret เก่าที่เก็บไว้ได้โดยไม่ต้องลองทีละตัว
#
# ของใหม่ผนึกเป็น v2 เสมอ · v1 ยังเปิดได้ตลอดไปและ **ไม่ถูกเขียนทับเอง** — รุ่นเก่าอ่าน v2
# ไม่ออก การแปลงแถวที่ยังเปิดได้อยู่แล้วจึงมีแต่ทำให้ถอยรุ่นลำบากขึ้นโดยไม่ได้อะไรกลับมา
FORMAT = "v2"
LEGACY_FORMAT = "v1"
NONCE_BYTES = 12
KDF_ROUNDS = 200_000

# สถานะของสำเนาหนึ่งใบ — ค่าเดียวกับที่ออกไปใน API (`seal_state`)
NONE = "none"            # ไม่มีสำเนา: ออกก่อนเปิดฟีเจอร์ เก็บแค่ hash
CURRENT = "current"      # เปิดได้ด้วย secret ปัจจุบัน
PREVIOUS = "previous"    # เปิดได้ด้วยตัวเก่าเท่านั้น — รอผนึกใหม่
LOST = "lost"            # มีสำเนา แต่ secret ที่ตั้งอยู่เปิดไม่ได้สักตัว
OFF = "off"              # มีสำเนา แต่ฟีเจอร์ปิดอยู่ (ไม่ได้ตั้ง secret ปัจจุบัน)

# เหตุที่เป็น LOST — ทางออกของแต่ละเหตุไม่เหมือนกัน จึงต้องแยก
WRONG_SECRET = "unknown_secret"    # v2: ป้ายไม่ตรงกับ secret ที่ตั้งอยู่ — เอาตัวเดิมกลับมาแล้วเปิดได้
DAMAGED = "damaged"                # v2: ป้ายตรง แต่เนื้อเปิดไม่ออก — secret ไหนก็ไม่ช่วย
UNREADABLE = "unreadable"          # v1: ไม่มีป้าย แยกสองเหตุข้างบนไม่ได้
NEWER_FORMAT = "unknown_format"    # รูปแบบที่รุ่นนี้ไม่รู้จัก — น่าจะถอยรุ่นลงมา


@dataclass(frozen=True)
class _Secret:
    cipher: AESGCM
    key_id: str


@dataclass(frozen=True)
class Opened:
    """ผลของการลองเปิดหนึ่งใบ: อยู่สถานะไหน และถ้าเปิดไม่ได้ เพราะอะไร"""

    state: str
    # ไม่อยู่ใน repr — ค่านี้ไปโผล่ใน log หรือ traceback ไม่ได้
    plaintext: str | None = field(default=None, repr=False)
    # ป้ายของ secret ที่ผนึกใบนี้ · None เมื่อไม่รู้ (v1 ที่เปิดไม่ออก)
    key_id: str | None = None
    reason: str = ""


@lru_cache(maxsize=8)
def _derive(secret: str) -> _Secret:
    """secret ที่คนตั้ง → คีย์ 256 บิต กับป้ายสั้น ๆ ที่ใช้เรียกมัน

    จำผลไว้เพราะ PBKDF2 สองแสนรอบกินราว 20 ms และเดิมถูกคิดใหม่ **ทุกครั้งที่เรียก** —
    หน้ารายการ key ถาม `reveal_enabled()` หนึ่งครั้งต่อใบ event loop จึงค้างตามจำนวน key
    """
    # ยืดรหัสผ่านที่คนพิมพ์เองให้เป็นคีย์ 256 บิต · salt คงที่ได้เพราะ secret นี้
    # ไม่ใช่รหัสผ่านของผู้ใช้และไม่ถูกนำไปเทียบกับฐานข้อมูลรหัสผ่านที่ไหน
    material = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"litegate-key-reveal", KDF_ROUNDS)
    # ป้ายมาจากการยืดอีกรอบด้วย salt คนละตัว จึงไม่มีบิตไหนร่วมกับคีย์ที่ใช้เข้ารหัส
    #
    # ป้ายถูกเก็บคู่กับของที่ผนึก คนที่ได้ฐานไปจึงเห็นมัน — แต่ไม่ได้อะไรเพิ่ม: จะเดา
    # secret จากป้ายต้องจ่าย PBKDF2 เท่ากับเดาจาก ciphertext ที่เขามีอยู่แล้ว และป้าย 32 บิต
    # ยืนยันคำเดาได้ *แย่กว่า* tag 128 บิตของ GCM เสียอีก
    label = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"litegate-key-reveal-id", KDF_ROUNDS)
    return _Secret(AESGCM(material), label[:4].hex())


def _current() -> _Secret | None:
    """secret ที่ใช้ผนึกของใหม่ · None = ปิดอยู่ ซึ่งเป็นค่าตั้งต้น"""
    secret = (get_settings().key_reveal_secret or "").strip()
    return _derive(secret) if secret else None


def _previous() -> _Secret | None:
    """secret ตัวก่อน ใช้เปิดอย่างเดียวระหว่างเปลี่ยนผ่าน

    None เมื่อไม่ได้ตั้ง · เมื่อฟีเจอร์ปิดอยู่ (ตัวเก่าตัวเดียวต้องไม่เปิดฟีเจอร์เอง) ·
    หรือเมื่อเป็นค่าเดียวกับตัวปัจจุบัน ซึ่งเท่ากับไม่มีตัวเก่า
    """
    settings = get_settings()
    current = (settings.key_reveal_secret or "").strip()
    previous = (settings.key_reveal_secret_previous or "").strip()
    if not current or not previous or previous == current:
        return None
    return _derive(previous)


def reveal_enabled() -> bool:
    """เปิดให้เรียกดู key เดิมได้ไหม — หน้าเว็บถามก่อนวาดปุ่ม"""
    return _current() is not None


def previous_configured() -> bool:
    """มีค่าอยู่ใน GW_KEY_REVEAL_SECRET_PREVIOUS ไหม — ไม่ว่าจะถูกนำไปใช้จริงหรือไม่"""
    return bool((get_settings().key_reveal_secret_previous or "").strip())


def previous_same_as_current() -> bool:
    settings = get_settings()
    current = (settings.key_reveal_secret or "").strip()
    return bool(current) and current == (settings.key_reveal_secret_previous or "").strip()


def key_ids() -> tuple[str | None, str | None]:
    """ป้ายของ (ตัวปัจจุบัน, ตัวเก่า) · None = ไม่ได้ตั้ง หรือไม่ได้ถูกใช้"""
    current, previous = _current(), _previous()
    return (current.key_id if current else None, previous.key_id if previous else None)


def key_id_of(secret: str) -> str:
    """ป้ายของ secret ที่ส่งมา — ให้ผู้ดูแลเทียบ secret เก่าที่เก็บไว้กับป้ายของใบที่เปิดไม่ได้"""
    return _derive(secret.strip()).key_id


def seal(plaintext: str) -> str:
    """ปิดผนึกไว้อ่านทีหลัง · คืนค่าว่างเมื่อปิดใช้อยู่ = ไม่เก็บอะไรเลย

    ผนึกใต้ secret ปัจจุบันเสมอ ไม่เคยใต้ตัวเก่า
    """
    current = _current()
    if current is None or not plaintext:
        return ""
    nonce = os.urandom(NONCE_BYTES)
    header = f"{FORMAT}:{current.key_id}"
    # ป้ายถูกผูกเข้ากับเนื้อ (AAD) — แก้ป้ายในฐานให้ชี้ secret อื่นแล้วเนื้อจะเปิดไม่ออก
    # รายงานสถานะจึงเชื่อป้ายได้เท่ากับที่เชื่อเนื้อ
    blob = nonce + current.cipher.encrypt(nonce, plaintext.encode(), header.encode())
    return f"{header}:{base64.urlsafe_b64encode(blob).decode()}"


def _open(secret: _Secret, payload: str, aad: bytes | None) -> str | None:
    try:
        blob = base64.urlsafe_b64decode(payload.encode())
        return secret.cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad).decode()
    except (InvalidTag, ValueError):
        # secret ไม่ใช่ตัวที่ผนึก หรือข้อมูลเสีย — ทั้งคู่แปลว่าอ่านไม่ได้ ไม่ใช่ว่าระบบพัง
        return None


def inspect(sealed: str | None) -> Opened:
    """ลองเปิดหนึ่งใบ แล้วบอกว่าอยู่สถานะไหน — ตัวปัจจุบันก่อน แล้วค่อยตัวเก่า

    สถานะมาจากการ **เปิดจริง** ไม่ใช่จากการอ่านป้ายอย่างเดียว: ใบที่ป้ายถูกแต่เนื้อเสีย
    ต้องไม่ถูกรายงานว่าเปิดได้ · ป้ายใช้ตอบคำถามถัดไปคือ "แล้วเปิดไม่ได้เพราะอะไร"
    """
    if not sealed:
        return Opened(NONE)
    version, _, rest = sealed.partition(":")
    recorded: str | None = None
    payload = rest
    if version == FORMAT:
        recorded, _, payload = rest.partition(":")
    current = _current()
    if current is None:
        return Opened(OFF, key_id=recorded or None)

    candidates = [(CURRENT, current)]
    previous = _previous()
    if previous is not None:
        candidates.append((PREVIOUS, previous))

    if version == FORMAT:
        aad = f"{FORMAT}:{recorded}".encode()
        matched = False
        for state, secret in candidates:
            if secret.key_id != recorded:
                continue
            matched = True
            plaintext = _open(secret, payload, aad)
            if plaintext is not None:
                return Opened(state, plaintext, recorded)
        return Opened(LOST, key_id=recorded or None,
                      reason=DAMAGED if matched else WRONG_SECRET)
    if version == LEGACY_FORMAT:
        for state, secret in candidates:
            plaintext = _open(secret, payload, None)
            if plaintext is not None:
                return Opened(state, plaintext, secret.key_id)
        return Opened(LOST, reason=UNREADABLE)
    return Opened(LOST, reason=NEWER_FORMAT)


def unseal(sealed: str) -> str | None:
    """เปิดผนึก · None เมื่อเปิดไม่ได้ ไม่ว่าด้วยเหตุใด

    เหตุที่เปิดไม่ได้มีหลายแบบ: ไม่มีของผนึกไว้ (key เก่าที่ออกก่อนเปิดฟีเจอร์), ฟีเจอร์
    ถูกปิดอยู่, หรือ secret ที่ตั้งอยู่ไม่ใช่ตัวที่ผนึก · ตัวเรียกที่ต้องบอกผู้ใช้ว่าเพราะอะไร
    ให้ใช้ `inspect` ซึ่งคืนสถานะและเหตุมาด้วย
    """
    return inspect(sealed).plaintext
