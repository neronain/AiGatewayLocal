"""Quota and rate limiting (PRD §10, FR-20..FR-24).

Two enforcement points per request:

  * `check()`  before forwarding - reads counters, rejects with 429 if a limit
    is already reached.
  * `record()` after completion  - increments counters with actual usage.

This is check-then-record, not reserve-then-settle: under a concurrent burst a
member can overshoot by at most (in-flight requests x per-request cost). That
is accepted deliberately (NFR-Q1) because reserving would require holding a
lock across a multi-minute generation. Overrun self-corrects on the next check.

Counters live in Redis when configured (shared across workers) and fall back to
the database otherwise, which is correct for a single-worker deployment.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode, GatewayError
from app.db.models import QuotaCounter, QuotaPolicy
from app.registry.schema import QuotaDefaults

log = logging.getLogger(__name__)


@dataclass
class Consumption:
    requests: int = 0
    text_input_tokens: int = 0
    visual_input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0

    @property
    def input_tokens(self) -> int:
        return self.text_input_tokens + self.visual_input_tokens


@dataclass
class ResolvedLimits:
    window: str
    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    max_images: int
    source: str  # user | workspace | global | default


# Months a "term" starts on. The default is a Thai academic year, because that
# is where this ran first; any organisation with its own calendar - fiscal
# quarters, semesters, sprints - sets its own in gateway.yaml. Nothing else in
# the gateway assumes a sector.
DEFAULT_TERM_START_MONTHS = (1, 6, 8)


def _term_bounds(now: datetime, starts: tuple[int, ...]) -> tuple[datetime, datetime]:
    """The term containing `now`, given the months terms begin on."""
    months = sorted({m for m in starts if 1 <= m <= 12}) or [1]
    begins = [m for m in months if m <= now.month]
    start_month = begins[-1] if begins else months[-1]
    start_year = now.year if begins else now.year - 1

    later = [m for m in months if m > start_month]
    if later:
        end_month, end_year = later[0], start_year
    else:
        end_month, end_year = months[0], start_year + 1

    start = datetime(start_year, start_month, 1, tzinfo=now.tzinfo)
    end = datetime(end_year, end_month, 1, tzinfo=now.tzinfo)
    return start, end


def window_bounds(
    window: str,
    now: datetime | None = None,
    term_start_months: tuple[int, ...] | None = None,
) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC)
    if window == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = monthrange(now.year, now.month)[1]
        return start, start + timedelta(days=last_day)
    if window == "term":
        return _term_bounds(now, term_start_months or DEFAULT_TERM_START_MONTHS)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


class CounterStore(ABC):
    @abstractmethod
    async def get(self, key: str, window: str) -> Consumption: ...

    @abstractmethod
    async def increment(self, key: str, window: str, delta: Consumption) -> None: ...


class DatabaseCounterStore(CounterStore):
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def get(self, key: str, window: str) -> Consumption:
        start, end = window_bounds(window)
        async with self._session_factory() as session:
            row = await self._fetch(session, key, start)
        if row is None:
            return Consumption()
        return Consumption(
            requests=row.requests,
            text_input_tokens=row.text_input_tokens,
            visual_input_tokens=row.visual_input_tokens,
            output_tokens=row.output_tokens,
            images=row.images,
        )

    async def increment(self, key: str, window: str, delta: Consumption) -> None:
        start, end = window_bounds(window)
        async with self._session_factory() as session:
            row = await self._fetch(session, key, start)
            if row is None:
                # Column defaults are applied at INSERT, so a freshly constructed
                # row has None counters. Seed them explicitly or the += below
                # raises and every increment is silently lost.
                row = QuotaCounter(
                    subject_key=key,
                    window_start=start,
                    window_end=end,
                    requests=0,
                    text_input_tokens=0,
                    visual_input_tokens=0,
                    output_tokens=0,
                    images=0,
                )
                session.add(row)
            row.requests += delta.requests
            row.text_input_tokens += delta.text_input_tokens
            row.visual_input_tokens += delta.visual_input_tokens
            row.output_tokens += delta.output_tokens
            row.images += delta.images
            await session.commit()

    @staticmethod
    async def _fetch(session: AsyncSession, key: str, start: datetime):
        result = await session.execute(
            select(QuotaCounter).where(
                QuotaCounter.subject_key == key,
                QuotaCounter.window_start == start,
            )
        )
        return result.scalar_one_or_none()


class RedisCounterStore(CounterStore):
    """Hash per (subject, window) with a TTL that expires at window end."""

    FIELDS = ("requests", "text_input_tokens", "visual_input_tokens", "output_tokens", "images")

    def __init__(self, redis) -> None:
        self._redis = redis

    @staticmethod
    def _redis_key(key: str, start: datetime) -> str:
        return f"quota:{key}:{start.isoformat()}"

    async def get(self, key: str, window: str) -> Consumption:
        start, _ = window_bounds(window)
        values = await self._redis.hgetall(self._redis_key(key, start))
        if not values:
            return Consumption()
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in values.items()
        }
        return Consumption(**{f: decoded.get(f, 0) for f in self.FIELDS})

    async def increment(self, key: str, window: str, delta: Consumption) -> None:
        start, end = window_bounds(window)
        redis_key = self._redis_key(key, start)
        pipe = self._redis.pipeline()
        for field_name in self.FIELDS:
            value = getattr(delta, field_name)
            if value:
                pipe.hincrby(redis_key, field_name, value)
        ttl = max(int((end - datetime.now(UTC)).total_seconds()), 60)
        pipe.expire(redis_key, ttl)
        await pipe.execute()


class QuotaService:
    def __init__(self, store: CounterStore, defaults: QuotaDefaults) -> None:
        self._store = store
        self._defaults = defaults

    def update_defaults(self, defaults: QuotaDefaults) -> None:
        self._defaults = defaults

    @staticmethod
    def subject_key(user_id: str, model_alias: str | None = None) -> str:
        return f"user:{user_id}:model:{model_alias}" if model_alias else f"user:{user_id}"

    async def resolve_limits(
        self,
        session: AsyncSession,
        user_id: str,
        workspace_id: str | None,
        model_alias: str,
    ) -> ResolvedLimits:
        """Most specific policy wins: user+model > user > workspace+model > workspace > global."""
        result = await session.execute(
            select(QuotaPolicy).where(QuotaPolicy.enabled.is_(True))
        )
        policies = list(result.scalars())

        def score(policy: QuotaPolicy) -> int:
            if policy.user_id and policy.user_id != user_id:
                return -1
            if policy.workspace_id and policy.workspace_id != workspace_id:
                return -1
            if policy.model_alias and policy.model_alias != model_alias:
                return -1
            value = 0
            if policy.user_id:
                value += 4
            if policy.workspace_id:
                value += 2
            if policy.model_alias:
                value += 1
            return value

        best: QuotaPolicy | None = None
        best_score = -1
        for policy in policies:
            current = score(policy)
            if current > best_score:
                best, best_score = policy, current

        if best is None or best_score < 0:
            d = self._defaults
            return ResolvedLimits(
                window=d.window,
                max_requests=d.max_requests,
                max_input_tokens=d.max_input_tokens,
                max_output_tokens=d.max_output_tokens,
                max_images=d.max_images,
                source="default",
            )
        return ResolvedLimits(
            window=best.window,
            max_requests=best.max_requests,
            max_input_tokens=best.max_input_tokens,
            max_output_tokens=best.max_output_tokens,
            max_images=best.max_images,
            source=best.scope,
        )

    async def check(self, user_id: str, limits: ResolvedLimits) -> Consumption:
        key = self.subject_key(user_id)
        used = await self._store.get(key, limits.window)

        def exceeded(name: str, used_value: int, limit: int) -> None:
            if limit and used_value >= limit:
                raise GatewayError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"Your {limits.window} {name} quota is exhausted "
                    f"({used_value:,} of {limit:,}). It resets at the start of the "
                    f"next {limits.window}.",
                    retry_after=_seconds_to_reset(limits.window),
                    details={
                        "quota": name,
                        "used": used_value,
                        "limit": limit,
                        "window": limits.window,
                        "resets_at": window_bounds(limits.window)[1].isoformat(),
                    },
                )

        exceeded("request", used.requests, limits.max_requests)
        exceeded("input token", used.input_tokens, limits.max_input_tokens)
        exceeded("output token", used.output_tokens, limits.max_output_tokens)
        exceeded("image", used.images, limits.max_images)
        return used

    async def record(self, user_id: str, window: str, delta: Consumption) -> None:
        try:
            await self._store.increment(self.subject_key(user_id), window, delta)
        except Exception:
            # Never fail a completed request because bookkeeping failed.
            log.exception("failed to record quota consumption for user %s", user_id)

    async def usage_snapshot(self, user_id: str, limits: ResolvedLimits) -> dict:
        used = await self._store.get(self.subject_key(user_id), limits.window)
        start, end = window_bounds(limits.window)
        return {
            "window": limits.window,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "limits": {
                "max_requests": limits.max_requests,
                "max_input_tokens": limits.max_input_tokens,
                "max_output_tokens": limits.max_output_tokens,
                "max_images": limits.max_images,
            },
            "used": {
                "requests": used.requests,
                "text_input_tokens": used.text_input_tokens,
                "visual_input_tokens": used.visual_input_tokens,
                "input_tokens": used.input_tokens,
                "output_tokens": used.output_tokens,
                "images": used.images,
            },
        }


def _seconds_to_reset(window: str) -> int:
    _, end = window_bounds(window)
    return max(int((end - datetime.now(UTC)).total_seconds()), 1)
