"""Process-wide services, wired once at startup and reachable from any route."""

from __future__ import annotations

import logging

from fastapi import Request

from app.config import Settings, get_settings
from app.core.perf import PerfStore
from app.core.quota import (
    CounterStore,
    DatabaseCounterStore,
    QuotaService,
    RedisCounterStore,
    ResilientCounterStore,
)
from app.core.routing import Router
from app.core.secrets import SecretStore
from app.core.usage import UsageRecorder
from app.db.session import get_sessionmaker
from app.registry.store import RegistryStore

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = RegistryStore(
            settings.config_dir, settings.registry_reload_seconds
        )
        self.secrets = SecretStore(settings.secrets_file)
        self.router = Router(self.registry)
        self.usage = UsageRecorder()
        # ความเร็วที่วัดได้จากทราฟฟิกจริง — ใช้จัดอันดับให้ model="auto"
        self.perf = PerfStore()
        self.redis = None
        self.counter_store: CounterStore = DatabaseCounterStore(get_sessionmaker())
        self.quota = QuotaService(
            self.counter_store, self.registry.snapshot.gateway.quota_defaults
        )
        self.started_at: float = 0.0
        # None = ปิดอยู่ · เส้นทางคำขอเช็ค `is not None` ก่อนใช้เสมอ
        self.response_cache = None

    async def start(self) -> None:
        await self.registry.start()
        self.quota.update_defaults(self.registry.snapshot.gateway.quota_defaults)

        if self.settings.redis_url:
            try:
                import redis.asyncio as aioredis

                self.redis = aioredis.from_url(
                    self.settings.redis_url, decode_responses=False
                )
                await self.redis.ping()
                # Wrapped, not swapped in: choosing the store once at startup
                # only survives the outage that already exists when you boot.
                # Redis dying later left every request failing with a 500, which
                # is a poor return on a component that is here for speed.
                self.counter_store = ResilientCounterStore(
                    RedisCounterStore(self.redis), DatabaseCounterStore(get_sessionmaker())
                )
                self.quota = QuotaService(
                    self.counter_store, self.registry.snapshot.gateway.quota_defaults
                )
                # ตัวนับคำขอที่กำลังวิ่งต้องแชร์ข้าม worker ด้วย ไม่งั้น max_concurrency
                # แปลว่า "N ต่อ worker" ซึ่งกับ 4 worker คือ 4N ตัวพร้อมกันที่ backend
                from app.core.inflight import (
                    LocalInFlightLimiter,
                    RedisInFlightLimiter,
                    ResilientInFlightLimiter,
                )

                self.router.set_limiter(
                    ResilientInFlightLimiter(
                        RedisInFlightLimiter(self.redis), LocalInFlightLimiter()
                    )
                )
                log.info("quota counters และ in-flight limit backed by Redis, "
                         "with database/local fallback")
            except Exception as exc:
                # Redis is already down. The database store is correct on its
                # own - slower and single-writer, but correct.
                log.error("Redis unavailable (%s); using database counters", exc)
                self.redis = None

        unshared = unshared_limit_warning(
            redis=self.redis is not None,
            workers=self.settings.workers,
            is_production=self.settings.is_production,
        )
        if unshared:
            log.warning("%s", unshared)

        if self.settings.response_cache:
            from app.core.responsecache import ResponseCache

            # แชร์ผ่าน Redis เมื่อมี ไม่งั้นแคชในเครื่อง (hit เฉพาะ worker ที่เคยตอบ)
            self.response_cache = ResponseCache(self.redis)
            log.info(
                "response cache เปิดอยู่ (%s · TTL 300 วิ · เฉพาะ temperature=0 ไม่มี tools)",
                "Redis" if self.redis else "ในเครื่อง",
            )

        await self.usage.start()
        await self.router.start_health_checks()

    async def stop(self) -> None:
        await self.router.stop_health_checks()
        await self.usage.stop()
        await self.registry.stop()
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:
                log.warning("error closing redis connection", exc_info=True)


def unshared_limit_warning(*, redis: bool, workers: int, is_production: bool) -> str:
    """ข้อความเตือนเมื่อ max_concurrency จะไม่หมายความตามที่เขียนไว้ — "" เมื่อไม่มีปัญหา

    ตัวนับคำขอที่กำลังวิ่งอยู่แชร์ข้าม worker ได้ก็ต่อเมื่อมี Redis (ดู core/inflight.py)
    ไม่มีแล้วแต่ละ worker นับของตัวเอง `max_concurrency: 1` จึงแปลว่า "1 ต่อ worker"
    = N ตัวพร้อมกันจริงที่ backend — ตรงข้ามกับสิ่งเดียวที่ค่านั้นมีไว้ทำ และเป็นค่าที่
    คนตั้งตอนโมเดลใหญ่รับได้ทีละคำขอพอดี

    **เตือน ไม่ใช่ปฏิเสธไม่ให้สตาร์ต** — คนที่ตั้งใจรันหลาย worker โดยไม่สนใจ
    max_concurrency มีจริง และเกตเวย์ที่ไม่ยอมขึ้นตอนตี 3 แย่กว่าเกตเวย์ที่ขึ้นพร้อม
    บรรทัดที่บอกตรง ๆ ว่ากำลังเกิดอะไร

    แยกออกมาจาก start() เพราะ start() ต่อ Redis จริง เปิด health check จริง และเริ่ม
    background task — เงื่อนไขสามตัวนี้จึงเทสไม่ได้ถ้าไม่แยก
    """
    if redis or workers <= 1 or not is_production:
        return ""
    return (
        f"GW_WORKERS={workers} แต่ไม่มี GW_REDIS_URL — ตัวนับคำขอที่กำลังวิ่งไม่ถูกแชร์"
        f"ข้าม worker · max_concurrency: N ของทุก endpoint จะกลายเป็น N×{workers} "
        f"ที่ backend จริง ๆ · ตั้ง GW_REDIS_URL หรือลด GW_WORKERS=1"
    )


def get_state(request: Request) -> AppState:
    return request.app.state.services


def build_state() -> AppState:
    return AppState(get_settings())
