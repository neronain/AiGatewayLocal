"""Console sign-in: password hashing and session tokens.

Deliberately separate from API keys. A key authenticates a *program* and lives
in a config file; a password authenticates a *person* and lives in their head.
Using the key as the console credential - which is what the first console did -
forces people to paste a production credential into a browser, and leaves no way
to issue yourself a new key once the old one is revoked.

Both primitives here are stdlib. scrypt is memory-hard, which is what a password
needs; API keys use a plain HMAC instead because they are already 256 bits of
randomness and do not need stretching.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time

from app.config import get_settings

# Tuned so a single verification costs roughly 100 ms on a small server: slow
# enough to make guessing expensive, fast enough that signing in is not annoying.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32
# scrypt needs about 128 * N * r bytes. OpenSSL refuses above 32 MB unless told
# otherwise, and the default parameters here land just over that line.
_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2

SESSION_COOKIE = "litegate_session"

# The same gateway answers on https://host and on http://host:8080, and cookies
# are not scoped by port or scheme - both addresses see one cookie jar. The
# https cookie carries `Secure`, and a browser refuses to let an insecure page
# overwrite a Secure cookie of the same name: it drops the new one silently, so
# signing in over :8080 returns 200 and then every call says "no API key". Two
# names, one per scheme, means neither address can shadow the other.
SESSION_COOKIE_INSECURE = "litegate_session_http"
SESSION_TTL_SECONDS = 8 * 3600


def session_cookie_name(secure: bool) -> str:
    return SESSION_COOKIE if secure else SESSION_COOKIE_INSECURE


def read_session_cookie(cookies, secure: bool) -> str:
    """The cookie for this scheme, falling back to the other one.

    The fallback matters behind a proxy that terminates TLS: a request can
    arrive marked secure while the browser stored the cookie under the other
    name, and asking someone to sign in twice for that is not an explanation
    anyone wants to hear.
    """
    return (
        cookies.get(session_cookie_name(secure))
        or cookies.get(session_cookie_name(not secure))
        or ""
    )

MIN_PASSWORD_LENGTH = 10


class PasswordError(ValueError):
    """The password does not meet policy."""


def check_password_policy(password: str) -> None:
    """Length only.

    Composition rules (a digit, a symbol, ...) push people towards predictable
    substitutions without adding real entropy; length is what actually helps.
    """
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )


def hash_password(password: str) -> str:
    check_password_policy(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
        maxmem=_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. An account with no password can never sign in."""
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            (password or "").encode(),
            salt=_unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(digest_b64)),
            maxmem=128 * int(n) * int(r) * 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, _unb64(digest_b64))


# ── ฝั่ง async: scrypt ต้องไม่รันบน event loop ────────────────────────────────
#
# วัดบนเครื่องพัฒนา (Apple M-series, OpenSSL ของระบบ) ด้วยค่าที่ตั้งไว้ข้างบน:
# **scrypt หนึ่งครั้ง = ~41 ms** และคอมเมนต์ข้างบนตั้งใจให้เป็น ~100 ms บนเซิร์ฟเวอร์เล็ก
# ซึ่งเป็นค่าที่ *ถูกต้อง* สำหรับการ hash รหัสผ่าน — ไม่ใช่สิ่งที่ควรลดลง
#
# ปัญหาคือมันรันอยู่บน event loop เดียวกับสตรีมทุกเส้นใน worker นั้น · ใครสักคน
# กดเข้าสู่ระบบในคอนโซล = ทุกสตรีมที่กำลังไหลอยู่หยุดนิ่ง 41-100 ms พร้อมกันหมด
# ผู้ใช้เห็นเป็นคำตอบสะดุดเป็นช่วง ๆ โดยไม่มีอะไรใน log บอกว่าทำไม
#
# ย้ายเข้า thread แล้วได้ผลจริงเพราะ `hashlib.scrypt` **ปล่อย GIL** (เรียก OpenSSL)
# — วัดแล้ว: 4 ครั้งเรียงกัน 158 ms · 4 ครั้งพร้อมกันใน thread 53 ms
#
# นี่คือเงื่อนไขที่ทำให้การย้ายคุ้ม และเป็นเหตุผลที่ของอย่างอื่นในเกตเวย์ไม่ถูกย้ายตาม:
# งานที่ถือ GIL ไว้ (base64 decode, parse YAML) ย้ายไป thread แล้ว event loop ก็ยัง
# ไม่ได้รัน ได้แต่ค่า overhead ของการสลับ thread เพิ่มมาเปล่า ๆ
async def hash_password_async(password: str) -> str:
    """เหมือน hash_password ทุกประการ แต่ไม่หยุด event loop"""
    # ตรวจนโยบายก่อนแบบ inline — ราคาเท่ากับ len() และรหัสผ่านที่สั้นเกินจะได้ไม่ต้อง
    # เสีย thread ไปหนึ่งตัวเพื่อไปโดนปฏิเสธข้างใน
    check_password_policy(password)
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    """เหมือน verify_password ทุกประการ แต่ไม่หยุด event loop"""
    if not stored:
        return False
    return await asyncio.to_thread(verify_password, password, stored)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------
def issue_session(user_id: str, epoch: int, ttl: int = SESSION_TTL_SECONDS) -> str:
    """A signed, self-contained token. No server-side session table.

    `epoch` is the user's session_epoch, which is bumped on every password
    change - so changing a password signs out every existing session without
    needing anything to be stored or swept.
    """
    payload = {"sub": user_id, "epoch": epoch, "exp": int(time.time()) + ttl}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_b64(_sign(body))}"


def read_session(token: str) -> dict | None:
    """Return the payload, or None if the token is unusable for any reason."""
    try:
        body, signature = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(_b64(_sign(body)), signature):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def _sign(body: str) -> bytes:
    secret = get_settings().api_key_pepper.encode()
    return hmac.new(secret, body.encode(), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
