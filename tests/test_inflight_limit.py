"""max_concurrency ต้องหมายความตามชื่อ

บั๊กเดิมมีสองชั้น: เช็คกับเพิ่มค่าอยู่คนละที่ (มี await คั่น) และตัวนับแยกต่อ worker
เทสชุดนี้จับชั้นที่ 1 · ชั้นที่ 2 แก้ด้วยการย้ายใบจองไป Redis ซึ่งทดสอบด้วย fake redis
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.inflight import LocalInFlightLimiter, ResilientInFlightLimiter


@pytest.mark.anyio
async def test_only_the_limit_gets_through_even_when_everyone_asks_at_once():
    """หัวใจของบั๊ก: coroutine หลายตัวถามพร้อมกันตอนตัวนับยังเป็น 0"""
    limiter = LocalInFlightLimiter()

    async def contender(n: int) -> bool:
        await asyncio.sleep(0)          # ให้ทุกตัวมาจ่ออยู่ตรงนี้พร้อมกัน
        return await limiter.acquire("ep", 1, f"lease-{n}")

    results = await asyncio.gather(*(contender(i) for i in range(50)))
    assert sum(results) == 1, "limit=1 ต้องผ่านได้ตัวเดียวเท่านั้น"
    assert await limiter.count("ep") == 1


@pytest.mark.anyio
async def test_releasing_frees_the_slot_for_the_next_caller():
    limiter = LocalInFlightLimiter()
    assert await limiter.acquire("ep", 1, "a")
    assert not await limiter.acquire("ep", 1, "b")
    await limiter.release("ep", "a")
    assert await limiter.acquire("ep", 1, "b")


@pytest.mark.anyio
async def test_releasing_twice_is_harmless():
    """release อยู่ใน finally ที่อาจรันซ้ำได้ — ต้องไม่ทำให้ตัวนับติดลบหรือพัง"""
    limiter = LocalInFlightLimiter()
    await limiter.acquire("ep", 2, "a")
    await limiter.release("ep", "a")
    await limiter.release("ep", "a")
    assert await limiter.count("ep") == 0
    assert await limiter.acquire("ep", 2, "b")


@pytest.mark.anyio
async def test_a_dead_workers_lease_expires_instead_of_leaking(monkeypatch):
    """worker ตายกลางคำขอแล้วไม่มีใครคืนใบจอง — ช่องต้องว่างเองในที่สุด

    นี่คือเหตุผลที่ใช้ใบจองที่มีวันหมดอายุ ไม่ใช่ตัวนับ INCR/DECR ธรรมดา
    """
    import app.core.inflight as mod

    limiter = LocalInFlightLimiter()
    monkeypatch.setattr(mod, "LEASE_TTL_SECONDS", 0.05)
    assert await limiter.acquire("ep", 1, "ตายไปแล้ว")
    assert not await limiter.acquire("ep", 1, "รายต่อไป")
    await asyncio.sleep(0.08)
    assert await limiter.acquire("ep", 1, "รายต่อไป"), "ใบจองที่หมดอายุต้องถูกกวาดทิ้ง"


@pytest.mark.anyio
async def test_it_keeps_working_when_redis_dies():
    """Redis ล่มแล้วทุกคำขอ 500 คือผลตอบแทนที่แย่จากคอมโพเนนต์ที่ใส่มาเพื่อความเร็ว"""
    class _Broken:
        async def acquire(self, *a): raise RuntimeError("redis หาย")
        async def release(self, *a): raise RuntimeError("redis หาย")
        async def count(self, *a): raise RuntimeError("redis หาย")

    limiter = ResilientInFlightLimiter(_Broken(), LocalInFlightLimiter())
    assert await limiter.acquire("ep", 1, "a") is True
    assert await limiter.acquire("ep", 1, "b") is False   # ยังบังคับเพดานอยู่
    await limiter.release("ep", "a")
    assert await limiter.acquire("ep", 1, "b") is True


# ── ระดับ Router: บั๊กจริงที่เจอ ──────────────────────────────────────────────

class _FakeEndpoint:
    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.max_concurrency = limit
        self.enabled = True
        self.priority = 1
        self.weight = 1
        self.normalized_base_url = "http://x"
        self.health_path = "/health"


@pytest.mark.anyio
async def test_router_lets_exactly_max_concurrency_through(monkeypatch):
    """เคสที่พังจริง: select() เช็คที่บรรทัดหนึ่ง แล้ว acquire() เพิ่มค่าอีกบรรทัดหนึ่ง
    โดยมี await คั่นกลาง — coroutine หลายตัวจึงผ่านด่านพร้อมกันตอนตัวนับยังเป็น 0
    """
    from app.core.errors import GatewayError
    from app.core.routing import Router

    class _Registry:
        class snapshot:
            class gateway:
                health_check_timeout_seconds = 1.0
                health_check_interval_seconds = 3600
            models: dict = {}

    router = Router(_Registry())
    endpoint = _FakeEndpoint("only-one", 1)

    granted, refused = 0, 0

    async def caller(n: int):
        nonlocal granted, refused
        await asyncio.sleep(0)      # จุดที่ทุกตัวมาจ่อพร้อมกัน — แทน await ที่คั่นอยู่จริง
        try:
            await router.acquire("m", endpoint, f"req-{n}")
        except GatewayError:
            refused += 1
            return
        granted += 1

    await asyncio.gather(*(caller(i) for i in range(30)))
    assert granted == 1, f"max_concurrency=1 แต่ผ่านไปได้ {granted} ตัว"
    assert refused == 29


@pytest.mark.anyio
async def test_router_frees_the_slot_after_release():
    from app.core.routing import Router

    class _Registry:
        class snapshot:
            class gateway:
                health_check_timeout_seconds = 1.0
                health_check_interval_seconds = 3600
            models: dict = {}

    router = Router(_Registry())
    endpoint = _FakeEndpoint("only-one", 1)

    await router.acquire("m", endpoint, "first")
    await router.release("m", endpoint, "first")
    await router.acquire("m", endpoint, "second")   # ต้องไม่โยน
    # ตัวเลขที่โชว์บนหน้า admin ต้องตามความจริงด้วย ไม่ใช่ค้างหรือติดลบ
    from app.core.routing import endpoint_key
    assert router._state.get(endpoint_key("m", endpoint)).in_flight == 1
