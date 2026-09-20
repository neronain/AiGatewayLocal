"""ตัวนับคำขอที่กำลังวิ่งอยู่ต่อ endpoint — ตัวที่ทำให้ max_concurrency หมายความตามชื่อ

ปัญหาเดิมมีสองชั้นซ้อนกัน และแก้ทีละชั้นไม่พอ
-------------------------------------------
**1. เช็คกับเพิ่มค่าอยู่คนละที่** `Router.select()` เช็ค `in_flight < max_concurrency`
ตอนเลือก endpoint แต่ `acquire()` เพิ่มค่าจริงอีกทีตอนจะยิง upstream — ระหว่างสองจุดนั้น
มี `await` คั่นหลายจุด (อ่าน body · resolve โมเดล · สร้าง payload) · บน event loop เดียว
coroutine หลายตัวจึงผ่านด่าน "ยังว่างอยู่" พร้อมกันได้ก่อนที่ใครจะเพิ่มค่าเป็นตัวแรก
ยิ่งคำขอมาพร้อมกันเยอะ ยิ่งผ่านไปได้เยอะ

**2. ตัวนับอยู่ในหน่วยความจำของแต่ละ process** รันด้วย 4 worker = ตัวนับ 4 ชุดที่ไม่รู้จักกัน
`max_concurrency: 1` จึงแปลว่า "1 ต่อ worker" = 4 ตัวพร้อมกันจริง ๆ ที่ backend

ชั้นที่ 2 คือเหตุผลว่าทำไมแค่ใส่ lock ใน process เดียวไม่พอ และชั้นที่ 1 คือเหตุผลว่าทำไม
แค่ย้ายตัวนับไป Redis เฉย ๆ ก็ไม่พอ — ต้อง "เช็คและจอง" ให้เป็นก้อนเดียวที่แบ่งไม่ได้

ทำไมเป็น ZSET ไม่ใช่ INCR/DECR
------------------------------
ตัวนับธรรมดารั่วถาวรเมื่อ worker ตายกลางคำขอ — ไม่มีใครเหลือมา DECR แล้วโควตานั้นหายไป
จนกว่าจะล้าง Redis ด้วยมือ · ZSET เก็บ "ใบจอง" ทีละใบโดยใช้เวลาหมดอายุเป็น score
ใบที่เจ้าของตายไปแล้วจะถูกกวาดทิ้งเองในรอบถัดไป ระบบจึงฟื้นตัวเองได้โดยไม่ต้องมีใครไปแตะ
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

# ใบจองมีอายุเท่าไรก่อนถูกถือว่าเป็นของ worker ที่ตายไปแล้ว
#
# ต้องยาวกว่าคำขอที่ช้าที่สุดที่ยังถือว่าปกติ ไม่งั้นสตรีมยาว ๆ จะถูกกวาดทิ้งทั้งที่ยังวิ่งอยู่
# แล้วปล่อยให้มีคำขอเกินโควตาเข้ามา · แต่ยิ่งยาว ช่องที่ค้างหลัง worker ตายก็ยิ่งว่างช้า
# 15 นาทีคือจุดที่รับได้ทั้งสองทาง — สตรีมที่นานกว่านี้แปลว่ามีอย่างอื่นผิดอยู่แล้ว
LEASE_TTL_SECONDS = 900


class InFlightLimiter(ABC):
    """เช็คและจองเป็นก้อนเดียว — ไม่มี API ให้ "เช็คก่อนแล้วค่อยจอง" โดยตั้งใจ"""

    @abstractmethod
    async def acquire(self, key: str, limit: int, lease: str) -> bool:
        """True = จองได้ · False = เต็ม (ไม่จองอะไรเลย)"""

    @abstractmethod
    async def release(self, key: str, lease: str) -> None:
        """คืนใบจอง — ต้องเรียกได้ซ้ำโดยไม่พัง"""

    @abstractmethod
    async def count(self, key: str) -> int:
        """จำนวนที่กำลังวิ่งอยู่ ณ ตอนนี้ — ใช้แสดงผลเท่านั้น ห้ามใช้ตัดสินใจ"""


class LocalInFlightLimiter(InFlightLimiter):
    """สำหรับ worker เดียว หรือตอนที่ยังไม่ได้ตั้ง Redis

    แก้ชั้นที่ 1 ได้ครบ (เช็คกับจองอยู่ใต้ lock เดียวกัน) แต่แก้ชั้นที่ 2 ไม่ได้ —
    หลาย process ยังต่างคนต่างนับ · ถ้ารันหลาย worker และต้องการให้ตัวเลขหมายความตามชื่อจริง
    ต้องตั้ง GW_REDIS_URL
    """

    def __init__(self) -> None:
        self._leases: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()

    def _sweep(self, key: str, now: float) -> dict[str, float]:
        live = {k: v for k, v in self._leases.get(key, {}).items() if v > now}
        self._leases[key] = live
        return live

    async def acquire(self, key: str, limit: int, lease: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            live = self._sweep(key, now)
            if len(live) >= limit:
                return False
            live[lease] = now + LEASE_TTL_SECONDS
            return True

    async def release(self, key: str, lease: str) -> None:
        async with self._lock:
            self._leases.get(key, {}).pop(lease, None)

    async def count(self, key: str) -> int:
        async with self._lock:
            return len(self._sweep(key, time.monotonic()))


# เช็ค · กวาดใบหมดอายุ · จอง — ทั้งหมดในสคริปต์เดียวที่ Redis รันแบบแบ่งไม่ได้
# แยกเป็นสามคำสั่งเมื่อไร ช่องว่างระหว่างคำสั่งก็กลายเป็นบั๊กเดิมที่เพิ่งแก้ไป
_ACQUIRE_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local ttl   = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local lease = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZCARD', key) >= limit then
  return 0
end
redis.call('ZADD', key, now + ttl, lease)
redis.call('EXPIRE', key, ttl + 60)
return 1
"""


class RedisInFlightLimiter(InFlightLimiter):
    """ใบจองอยู่ใน Redis — ทุก worker และทุกเครื่องเห็นตัวเลขเดียวกัน"""

    def __init__(self, redis) -> None:
        self._redis = redis
        self._script = redis.register_script(_ACQUIRE_LUA)

    @staticmethod
    def _key(key: str) -> str:
        return f"inflight:{key}"

    async def acquire(self, key: str, limit: int, lease: str) -> bool:
        now = time.time()
        got = await self._script(
            keys=[self._key(key)], args=[now, LEASE_TTL_SECONDS, limit, lease]
        )
        return bool(int(got))

    async def release(self, key: str, lease: str) -> None:
        await self._redis.zrem(self._key(key), lease)

    async def count(self, key: str) -> int:
        await self._redis.zremrangebyscore(self._key(key), "-inf", time.time())
        return int(await self._redis.zcard(self._key(key)))


class ResilientInFlightLimiter(InFlightLimiter):
    """Redis ตราบที่มันตอบ · ตกกลับมาที่ในเครื่องเมื่อมันล่ม

    เหตุผลเดียวกับ ResilientCounterStore ของโควตา: Redis ล่มแล้วทุกคำขอ 500 คือผลตอบแทน
    ที่แย่มากจากคอมโพเนนต์ที่ใส่เข้ามาเพื่อความเร็ว · ตอน fallback เพดานจะกลายเป็น
    "ต่อ worker" ชั่วคราว ซึ่งหลวมกว่าที่ตั้งไว้ แต่ยังกันไม่ให้ backend ถูกถล่ม
    และดีกว่าปฏิเสธทุกคนทิ้ง
    """

    def __init__(self, primary: InFlightLimiter, fallback: InFlightLimiter) -> None:
        self._primary = primary
        self._fallback = fallback
        self._degraded = False

    async def _try(self, name: str, *args):
        try:
            result = await getattr(self._primary, name)(*args)
            if self._degraded:
                log.info("in-flight limiter กลับมาใช้ Redis ได้แล้ว")
                self._degraded = False
            return result
        except Exception as exc:
            if not self._degraded:
                log.error("in-flight limiter ตกไปนับในเครื่องชั่วคราว (%s)", exc)
                self._degraded = True
            return await getattr(self._fallback, name)(*args)

    async def acquire(self, key: str, limit: int, lease: str) -> bool:
        return await self._try("acquire", key, limit, lease)

    async def release(self, key: str, lease: str) -> None:
        # คืนทั้งสองฝั่งเสมอ — ระหว่าง degrade ใบจองอาจอยู่คนละที่กับที่คิด
        for store in (self._primary, self._fallback):
            try:
                await store.release(key, lease)
            except Exception:
                pass

    async def count(self, key: str) -> int:
        return await self._try("count", key)
