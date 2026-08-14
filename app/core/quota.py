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
import time
from abc import ABC, abstractmethod
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from prometheus_client import Gauge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode, GatewayError
from app.db.models import AccessGroup, QuotaCounter, QuotaPolicy
from app.registry.schema import QuotaDefaults

log = logging.getLogger(__name__)

# Falling back to database counters is not an outage - requests keep working -
# so it produces no errors and nothing in the logs anyone is watching. Which is
# how a gateway ends up running for a week on a Redis that died on Tuesday.
#
# Phrased as "degraded", not "redis up", so it reads correctly on the many
# deployments that never configure Redis at all: those are not degraded, they
# are small, and an alert that fires on every SQLite install is an alert
# everybody turns off. It goes to 1 only when a fallback actually happens.
QUOTA_DEGRADED = Gauge(
    "litegate_quota_counters_degraded",
    "1 when quota counting has fallen back from Redis to the database",
)


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
    # Burst control, counted per minute rather than per window. 0 is unlimited,
    # which is the default: a rate limit nobody chose is a rate limit that will
    # refuse somebody mid-lesson for a reason nobody can explain.
    max_requests_per_minute: int = 0
    max_tokens_per_minute: int = 0

    @property
    def rate_limited(self) -> bool:
        return bool(self.max_requests_per_minute or self.max_tokens_per_minute)


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
    # A per-minute window rides the same counters as the daily one. A day's
    # quota stops somebody using a term's worth in a week; it does nothing about
    # forty people pressing send at the start of a class, which is the shape of
    # the load this actually gets.
    if window == "minute":
        start = now.replace(second=0, microsecond=0)
        return start, start + timedelta(minutes=1)
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


class ResilientCounterStore(CounterStore):
    """Redis while it answers, the database when it does not (NFR-A3).

    The gateway used to decide once, at startup: ping Redis, and install either
    the Redis store or the database store for the process lifetime. That covers
    the outage that already exists when you boot, and nothing else. Redis dying
    an hour later left the Redis store in place, and every request - every
    request, not just quota reporting - failed with a 500.

    Which is worse than it sounds, because Redis is here as an optimisation.
    The database store is correct on its own; it is just slower and
    single-writer. Losing the cache should cost latency, not availability.

    Failing over per call would mean paying a connection timeout on every
    request for as long as the outage lasts, so a failure marks Redis down for
    `retry_seconds` and the calls in between go straight to the database.

    **On recovery, counts written during the outage are not lost.** They went to
    the database, which Redis knows nothing about, so the first Redis miss after
    an outage consults the database and seeds Redis from it. Without that, a
    Redis that restarts empty - a crash without persistence, an eviction, a
    `FLUSHALL` - would hand every member their whole quota back. A quota that
    resets itself whenever the cache hiccups is not a quota.

    The reseed fires on a *miss*, so it does not cover the case where Redis
    survives the outage holding a partial count: the two ledgers are then
    disjoint and the total is under-reported by whatever was spent while Redis
    was unreachable. That is deliberate. Merging them would need a distributed
    lock to stop several workers merging the same rows, and the failure that
    buys - double-counting, which blocks a member who has done nothing wrong -
    is worse than the one it fixes. Under-counting is bounded by the length of
    the outage and errs towards letting people work.
    """

    def __init__(
        self,
        redis_store: CounterStore,
        database_store: CounterStore,
        retry_seconds: float = 30.0,
    ) -> None:
        self._redis = redis_store
        self._database = database_store
        self._retry_seconds = retry_seconds
        self._down_until = 0.0
        # Set the first time we fall back, and never cleared: it marks that the
        # database may hold counts Redis has never seen, which is what makes the
        # reseed on a miss necessary rather than a permanent extra query.
        self._database_has_counts = False

    @property
    def using_redis(self) -> bool:
        return time.monotonic() >= self._down_until

    def _mark_down(self, exc: Exception) -> None:
        first = self.using_redis
        self._down_until = time.monotonic() + self._retry_seconds
        self._database_has_counts = True
        QUOTA_DEGRADED.set(1)
        if first:
            # Once per outage, not once per request: a Redis outage under load
            # would otherwise write more log than the outage is worth reading.
            log.error(
                "Redis unavailable (%s); quota counters fall back to the database "
                "for %.0fs", exc, self._retry_seconds,
            )

    async def get(self, key: str, window: str) -> Consumption:
        if self.using_redis:
            try:
                counted = await self._redis.get(key, window)
            except Exception as exc:  # redis client raises its own hierarchy
                self._mark_down(exc)
            else:
                QUOTA_DEGRADED.set(0)
                if counted != Consumption() or not self._database_has_counts:
                    return counted
                # Redis has nothing for this window but an earlier outage may
                # have put counts in the database. Take those and put them back
                # into Redis so the next read is fast again.
                stored = await self._database.get(key, window)
                if stored != Consumption():
                    try:
                        await self._redis.increment(key, window, stored)
                    except Exception as exc:
                        self._mark_down(exc)
                    log.info("reseeded Redis quota counters for %s from the database", key)
                return stored
        return await self._database.get(key, window)

    async def increment(self, key: str, window: str, delta: Consumption) -> None:
        if self.using_redis:
            try:
                await self._redis.increment(key, window, delta)
                QUOTA_DEGRADED.set(0)
                return
            except Exception as exc:
                self._mark_down(exc)
        await self._database.increment(key, window, delta)


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
        """Most specific policy wins.

        user+model > user+bundle > user > workspace+model > workspace+bundle >
        workspace > global. A policy naming one alias beats a policy naming a
        bundle that happens to contain it, for the same reason a rule about one
        person beats a rule about their class: it was written with more
        knowledge of the case.
        """
        result = await session.execute(
            select(QuotaPolicy).where(QuotaPolicy.enabled.is_(True))
        )
        policies = list(result.scalars())

        # Only the bundles some policy actually points at, and only when the
        # alias could match one - a deployment with no bundle quotas asks nothing.
        bundles: dict[str, set[str]] = {}
        wanted = {p.access_group_id for p in policies if p.access_group_id}
        if wanted and model_alias:
            rows = await session.execute(
                select(AccessGroup.id, AccessGroup.models).where(
                    AccessGroup.id.in_(wanted), AccessGroup.enabled.is_(True)
                )
            )
            bundles = {gid: set(models or []) for gid, models in rows}

        def score(policy: QuotaPolicy) -> int:
            if policy.user_id and policy.user_id != user_id:
                return -1
            if policy.workspace_id and policy.workspace_id != workspace_id:
                return -1
            if policy.model_alias and policy.model_alias != model_alias:
                return -1
            if policy.access_group_id and model_alias not in bundles.get(
                policy.access_group_id, set()
            ):
                return -1
            value = 0
            if policy.user_id:
                value += 8
            if policy.workspace_id:
                value += 4
            if policy.model_alias:
                value += 2
            elif policy.access_group_id:
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
            max_requests_per_minute=best.max_requests_per_minute or 0,
            max_tokens_per_minute=best.max_tokens_per_minute or 0,
        )

    async def check(self, user_id: str, limits: ResolvedLimits) -> Consumption:
        key = self.subject_key(user_id)
        used = await self._store.get(key, limits.window)

        def exceeded(name: str, used_value: int, limit: int, window: str) -> None:
            if not limit or used_value < limit:
                return
            wait = _seconds_to_reset(window)
            when = (
                f"It clears in {wait} second{'s' if wait != 1 else ''}."
                if window == "minute"
                else f"It resets at the start of the next {window}."
            )
            raise GatewayError(
                ErrorCode.QUOTA_EXCEEDED,
                f"Your {window} {name} quota is exhausted "
                f"({used_value:,} of {limit:,}). {when}",
                retry_after=wait,
                details={
                    "quota": name,
                    "used": used_value,
                    "limit": limit,
                    "window": window,
                    "resets_at": window_bounds(window)[1].isoformat(),
                },
            )

        # The burst check comes first. Both can be over at once, and being told
        # to wait forty seconds is a more useful answer than being told to come
        # back tomorrow when the daily figure was not the binding one.
        if limits.rate_limited:
            per_minute = await self._store.get(key, "minute")
            exceeded("request", per_minute.requests, limits.max_requests_per_minute, "minute")
            exceeded(
                "token",
                per_minute.input_tokens + per_minute.output_tokens,
                limits.max_tokens_per_minute,
                "minute",
            )

        exceeded("request", used.requests, limits.max_requests, limits.window)
        exceeded("input token", used.input_tokens, limits.max_input_tokens, limits.window)
        exceeded("output token", used.output_tokens, limits.max_output_tokens, limits.window)
        exceeded("image", used.images, limits.max_images, limits.window)
        return used

    async def record(
        self, user_id: str, window: str, delta: Consumption, *, rate_limited: bool = False
    ) -> None:
        try:
            key = self.subject_key(user_id)
            await self._store.increment(key, window, delta)
            # Only when a rate limit is actually set: otherwise every deployment
            # that never wanted one would pay for a second counter per request.
            if rate_limited:
                await self._store.increment(key, "minute", delta)
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
