"""แคชคำตอบแบบตรงตัวเป๊ะ — เฉพาะคำขอที่ผลลัพธ์ต้องเหมือนเดิมทุกครั้งเท่านั้น

ขอบเขตแคบโดยตั้งใจ
-------------------
แคชคำตอบของโมเดลเป็นเรื่องที่พลาดแล้วเจ็บ เพราะ "คำตอบผิดที่ดูน่าเชื่อ" แย่กว่า "ไม่มีคำตอบ"
โดยเฉพาะกับ coding agent ที่เอาผลไปแก้ไฟล์ต่อ · กติกาทุกข้อข้างล่างนี้จึงเป็นเงื่อนไขที่
**ต้องจริงพร้อมกันทั้งหมด** ไม่ใช่ตัวเลือก:

- `temperature` ต้องระบุมาและเป็น **0 เท่านั้น** — ไม่ระบุ = ใช้ค่า default ของ backend
  ซึ่งเราไม่รู้และเปลี่ยนได้ · อะไรที่ไม่ deterministic ห้ามแคช
- **ห้ามมี tools / functions** — คำตอบขึ้นกับสถานะภายนอกที่เราไม่เห็น
- `n` ต้องเป็น 1 — ขอหลายคำตอบแปลว่าตั้งใจอยากได้ความต่าง
- เฉพาะ HTTP 200 ที่ parse เป็น JSON ได้ · error ไม่แคช

tenant เป็น **prefix ของ key** ไม่ใช่ตัวกรองหลัง lookup
---------------------------------------------------
ข้อนี้สำคัญที่สุดในไฟล์ · ถ้าเก็บรวมกันแล้วค่อยกรองทีหลัง วันที่ใครลืมกรอง (หรือกรองผิดชั้น)
คือวันที่คำตอบของลูกค้า A ไปโผล่ที่ลูกค้า B · ทำเป็น prefix แล้วการรั่วข้ามองค์กรไม่ได้
"เกิดจากบั๊ก" แต่ต้อง "เขียนโค้ดผิดจนสร้าง key ของคนอื่น" ซึ่งยากกว่ามาก

ยังต้องหักโควตาทุกครั้งที่ hit
------------------------------
ไม่หัก = ลูกค้าถามคำถามเดิมซ้ำ ๆ ได้ฟรีไม่จำกัด ซึ่งเป็นช่องโหว่รายได้แบบเดียวกับที่เพิ่ง
ปิดไปใน 1.6.0 แค่คนละทาง · ผู้เรียกต้อง `finalize()` ด้วย usage ที่แคชไว้เสมอ
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from app.core import jsonio

log = logging.getLogger(__name__)

TTL_SECONDS = 300

# ฟิลด์ที่ไม่มีผลต่อเนื้อคำตอบ — ตัดออกจาก key เพื่อให้ hit rate ไม่พังเพราะ metadata
# ระวัง: ตัดอะไรที่ *มีผล* ออกไป = สองคำขอที่ต่างกันจริงใช้ key เดียวกัน = ตอบผิด
# ถ้าไม่แน่ใจว่าฟิลด์ไหนมีผลไหม ให้ **คงไว้** เสมอ
_IGNORED_FIELDS = frozenset({"stream", "stream_options", "user", "metadata"})


def _tenant_prefix(principal) -> str:
    """หน่วยที่แคชถูกแบ่งตาม — workspace ถ้ามี ไม่มีก็รายคน

    ใช้ workspace เมื่อมีเพราะคนในองค์กรเดียวกันถามซ้ำกันบ่อย แคชจึงมีประโยชน์จริง
    ส่วนคนที่ไม่ได้อยู่ workspace ไหนเลย แคชแยกรายคน — hit rate ต่ำกว่าแต่ปลอดภัยเสมอ
    """
    workspace = getattr(principal, "workspace_id", None)
    return f"w:{workspace}" if workspace else f"u:{principal.user_id}"


def cacheable_reason(payload: dict[str, Any]) -> str | None:
    """คืนเหตุผลที่ *แคชไม่ได้* — None แปลว่าแคชได้ (ใช้ใน log และเทส)"""
    temperature = payload.get("temperature")
    if temperature is None:
        return "ไม่ได้ระบุ temperature"
    try:
        if float(temperature) != 0.0:
            return f"temperature={temperature} ไม่ใช่ 0"
    except (TypeError, ValueError):
        return f"temperature อ่านค่าไม่ได้ ({temperature!r})"
    if payload.get("tools") or payload.get("functions") or payload.get("tool_choice"):
        return "คำขอมี tools"
    n = payload.get("n")
    if n is not None and int(n) != 1:
        return f"n={n} ไม่ใช่ 1"
    return None


def build_key(principal, *, alias: str, upstream_model: str, protocol: str,
              payload: dict[str, Any]) -> str | None:
    """key ของคำขอนี้ — None เมื่อไม่เข้าเงื่อนไขให้แคช

    `upstream_model` อยู่ใน key ด้วยเพราะ alias เดียวกันชี้ไปคนละ weights ได้เมื่อ
    routing เปลี่ยน · ไม่ใส่ = สลับ weights แล้วยังได้คำตอบของตัวเก่าไปอีก 5 นาที
    """
    if cacheable_reason(payload) is not None:
        return None
    material = {k: v for k, v in payload.items() if k not in _IGNORED_FIELDS}
    digest = hashlib.sha256(
        jsonio.canonical(
            {"alias": alias, "upstream": upstream_model, "protocol": protocol, "req": material},
            default=str,
        )
    ).hexdigest()
    return f"rc:{_tenant_prefix(principal)}:{digest}"


class ResponseCache:
    """Redis เมื่อมี · ในเครื่องเมื่อไม่มี · ไม่เคยทำให้คำขอล้มเพราะแคชมีปัญหา"""

    def __init__(self, redis=None, max_local: int = 512) -> None:
        self._redis = redis
        self._local: dict[str, tuple[float, str]] = {}
        self._max_local = max_local

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            if self._redis is not None:
                raw = await self._redis.get(key)
                if raw is None:
                    return None
                return jsonio.loads(raw)
            entry = self._local.get(key)
            if entry is None:
                return None
            expires, raw = entry
            if expires <= time.monotonic():
                self._local.pop(key, None)
                return None
            return jsonio.loads(raw)
        except Exception as exc:
            # แคชอ่านไม่ได้ = ถือว่า miss · ห้ามทำให้คำขอพังเพราะของที่มีไว้เร่งความเร็ว
            log.warning("อ่าน response cache ไม่ได้ (%s) — ถือเป็น miss", exc)
            return None

    async def put(self, key: str, value: dict[str, Any]) -> None:
        try:
            raw = jsonio.dumps(value)
            if self._redis is not None:
                await self._redis.set(key, raw, ex=TTL_SECONDS)
                return
            if len(self._local) >= self._max_local:
                # ตัดตัวที่ใกล้หมดอายุที่สุดทิ้งก่อน — ไม่ใช่ LRU เต็มรูปแบบ แต่พอสำหรับ
                # fallback ที่มีอายุ 5 นาทีอยู่แล้ว และไม่ต้องเก็บสถิติการใช้เพิ่ม
                oldest = min(self._local, key=lambda k: self._local[k][0])
                self._local.pop(oldest, None)
            self._local[key] = (time.monotonic() + TTL_SECONDS, raw)
        except Exception as exc:
            log.warning("เขียน response cache ไม่ได้ (%s) — ข้ามไป", exc)
