"""Losing Redis must cost latency, not availability (NFR-A3).

This was written after the claim was tested against a real deployment and
turned out to be false: with Redis stopped, every request returned 500. The
gateway chose its counter store once at startup, so it handled the outage that
already existed at boot and no other.
"""

from __future__ import annotations

import pytest

from app.core.quota import Consumption, ResilientCounterStore


class _Memory:
    """A counter store that remembers, and can be told to start failing."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], Consumption] = {}
        self.broken: Exception | None = None
        self.reads = 0
        self.writes = 0

    async def get(self, key: str, window: str) -> Consumption:
        if self.broken:
            raise self.broken
        self.reads += 1
        return self.counts.get((key, window), Consumption())

    async def increment(self, key: str, window: str, delta: Consumption) -> None:
        if self.broken:
            raise self.broken
        self.writes += 1
        current = self.counts.get((key, window), Consumption())
        self.counts[(key, window)] = Consumption(
            requests=current.requests + delta.requests,
            text_input_tokens=current.text_input_tokens + delta.text_input_tokens,
            visual_input_tokens=current.visual_input_tokens + delta.visual_input_tokens,
            output_tokens=current.output_tokens + delta.output_tokens,
            images=current.images + delta.images,
        )


def _store(retry: float = 30.0) -> tuple[ResilientCounterStore, _Memory, _Memory]:
    redis, database = _Memory(), _Memory()
    return ResilientCounterStore(redis, database, retry_seconds=retry), redis, database


ONE = Consumption(requests=1, text_input_tokens=10)


# ---------------------------------------------------------------------------
# The failure that started this
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_request_still_succeeds_when_redis_dies_mid_flight():
    store, redis, database = _store()
    await store.increment("u", "day", ONE)
    redis.broken = ConnectionError("Connection refused")

    await store.increment("u", "day", ONE)          # must not raise
    assert (await store.get("u", "day")).requests == 1
    assert database.writes == 1


@pytest.mark.asyncio
async def test_reading_a_quota_survives_redis_being_down():
    """`/v1/me` reads counters too, and broke for the same reason."""
    store, redis, _ = _store()
    redis.broken = ConnectionError("Connection refused")

    assert await store.get("u", "day") == Consumption()


# ---------------------------------------------------------------------------
# Not paying the timeout on every request
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_redis_is_left_alone_for_a_while_after_it_fails():
    """Retrying every request means paying a connect timeout on every request."""
    store, redis, _ = _store(retry=30.0)
    redis.broken = ConnectionError("down")

    for _ in range(5):
        await store.increment("u", "day", ONE)

    assert store.using_redis is False


@pytest.mark.asyncio
async def test_redis_is_tried_again_once_the_window_passes():
    store, redis, database = _store(retry=0.0)
    redis.broken = ConnectionError("down")
    await store.increment("u", "day", ONE)

    redis.broken = None
    await store.increment("u", "day", ONE)
    assert redis.writes == 1
    assert database.writes == 1


# ---------------------------------------------------------------------------
# Recovery must not hand quota back
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_counts_recorded_during_an_outage_survive_recovery():
    """A quota that resets whenever the cache hiccups is not a quota."""
    store, redis, database = _store(retry=0.0)
    redis.broken = ConnectionError("down")
    for _ in range(3):
        await store.increment("u", "day", ONE)
    assert database.counts[("u", "day")].requests == 3

    redis.broken = None
    assert (await store.get("u", "day")).requests == 3


@pytest.mark.asyncio
async def test_the_reseed_puts_the_counts_back_into_redis():
    """Otherwise every read for the rest of the window pays a database query."""
    store, redis, database = _store(retry=0.0)
    redis.broken = ConnectionError("down")
    await store.increment("u", "day", ONE)

    redis.broken = None
    await store.get("u", "day")
    assert redis.counts[("u", "day")].requests == 1


@pytest.mark.asyncio
async def test_a_healthy_gateway_never_touches_the_database():
    """The fallback must not cost anything while nothing is wrong."""
    store, redis, database = _store()

    await store.increment("u", "day", ONE)
    await store.get("u", "day")

    assert redis.writes == 1 and redis.reads == 1
    assert database.reads == 0 and database.writes == 0


@pytest.mark.asyncio
async def test_an_empty_window_is_not_a_reason_to_query_the_database():
    """A new window reads empty from Redis; that is normal, not an outage."""
    store, redis, database = _store()

    assert await store.get("fresh", "day") == Consumption()
    assert database.reads == 0
