"""Process-wide services, wired once at startup and reachable from any route."""

from __future__ import annotations

import logging

from fastapi import Request

from app.config import Settings, get_settings
from app.core.quota import (
    CounterStore,
    DatabaseCounterStore,
    QuotaService,
    RedisCounterStore,
    ResilientCounterStore,
)
from app.core.perf import PerfStore
from app.core.routing import Router
from app.core.usage import UsageRecorder
from app.db.session import get_sessionmaker
from app.core.secrets import SecretStore
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
                log.info("quota counters backed by Redis, with database fallback")
            except Exception as exc:
                # Redis is already down. The database store is correct on its
                # own - slower and single-writer, but correct.
                log.error("Redis unavailable (%s); using database counters", exc)
                self.redis = None

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


def get_state(request: Request) -> AppState:
    return request.app.state.services


def build_state() -> AppState:
    return AppState(get_settings())
