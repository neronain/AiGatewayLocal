"""A request must not hold a pooled connection while it streams (WS-4.1).

The bug this pins down is invisible when it works. Every in-flight request used
to keep one pooled database connection checked out for its entire lifetime,
because SQLAlchemy autobegins a transaction on the first `execute()` and FastAPI
closes a `yield` dependency only after the response body has been *sent*. For a
JSON reply that is microseconds. For a StreamingResponse it is the whole
generation - so a handful of slow streams could exhaust the pool and the next
caller got a 500, and on SQLite the open read transaction also pinned the WAL so
`gateway.db-wal` grew for as long as the longest stream ran.

The tests that matter here are the ones that would have passed before the fix
for the wrong reason, so each one probes the pool at a moment that is *inside*
the streaming path rather than after it:

  * `respx` side effects run inside the generator, because the upstream call is
    made there - that is the deterministic probe.
  * the mid-flight test additionally stops between SSE chunks, which is the
    state a real multi-minute generation spends all its time in.

The quota tests exist because releasing the connection early is only safe if
nothing after that point needs the request session. They run against both
counter stores on purpose: Redis is being enabled on the deployment, and a fix
that only holds when Redis is on is not a fix.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

UPSTREAM_CHAT = "http://dgx03:8000/v1/chat/completions"

CHUNKS = [
    'data: {"id":"1","choices":[{"delta":{"content":"he"},"index":0}]}\n\n',
    'data: {"id":"1","choices":[{"delta":{"content":"llo"},"index":0}]}\n\n',
    'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n',
    'data: {"id":"1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
    "data: [DONE]\n\n",
]

NON_STREAM_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "ucbye/Qwen3-Coder-Next-NVFP4-GB10",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def pool():
    """The live pool. `checkedout()` is a plain counter, safe to read from here.

    Verified rather than assumed: a file-backed SQLite URL gives an
    `AsyncAdaptedQueuePool`, and `AsyncEngine.pool` exposes the sync pool's
    `checkedout()` / `size()` directly - there is no `.sync_engine` hop.
    """
    from app.db.session import get_engine

    return get_engine().pool


def sse(**kwargs):
    return httpx.Response(
        200,
        text="".join(CHUNKS),
        headers={"content-type": "text/event-stream"},
        **kwargs,
    )


def stream_body(client, key: str, **extra):
    return client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth(key),
        json={
            "model": "coding",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            **extra,
        },
    )


# ---------------------------------------------------------------------------
# The connection is released before the body streams
# ---------------------------------------------------------------------------
@respx.mock
def test_no_connection_is_held_when_the_stream_reaches_upstream(client, member_key):
    """The probe runs inside the generator, which is the whole point.

    `upstream.stream_json` is called from inside `generator()`, so a respx side
    effect fires after the handler has returned and while the StreamingResponse
    is being produced. Before the fix this read 1: the request session was still
    sitting in its autobegun transaction, waiting for FastAPI to tear the
    dependency down once the last SSE byte had gone out.
    """
    seen: list[int] = []

    def record(_request):
        seen.append(pool().checkedout())
        return sse()

    respx.post(UPSTREAM_CHAT).mock(side_effect=record)

    with stream_body(client, member_key) as response:
        assert response.status_code == 200
        payload = "".join(response.iter_text())

    assert payload.rstrip().endswith("data: [DONE]")
    assert seen == [0], (
        f"a pooled connection was still checked out when the stream reached "
        f"upstream: {seen}"
    )


class ProbingStream(httpx.AsyncByteStream):
    """An upstream body that records the pool state between SSE chunks.

    Iterating this happens inside the gateway's streaming generator, so each
    sample is taken at the exact moment a real generation spends its minutes:
    partway through the response, with the client still connected.

    Driving it from the test thread instead does not work - `TestClient` buffers
    the response, so by the time `iter_text()` yields anything the generator has
    already finished and the dependency has been torn down. A test written that
    way passes with or without the fix, which is worse than no test.
    """

    def __init__(self, chunks: list[str], seen: list[int]) -> None:
        self._chunks = chunks
        self.seen = seen

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk.encode()
            # Hand control back so the gateway forwards what it just read.
            await asyncio.sleep(0)
            self.seen.append(pool().checkedout())


@respx.mock
def test_no_connection_is_held_between_chunks(client, member_key):
    """Mid-flight, sampled from inside the stream loop after every chunk."""
    seen: list[int] = []
    respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(
            200,
            stream=ProbingStream(CHUNKS, seen),
            headers={"content-type": "text/event-stream"},
        )
    )

    with stream_body(client, member_key) as response:
        payload = "".join(response.iter_text())

    assert payload.rstrip().endswith("data: [DONE]")
    assert len(seen) >= len(CHUNKS) - 1, f"stream ended early: {seen}"
    assert set(seen) == {0}, (
        f"a connection was checked out partway through the stream: {seen}"
    )


@respx.mock
def test_no_connection_is_held_during_a_non_streaming_upstream_call(client, member_key):
    """The non-streaming path waits on upstream *inside* the handler.

    `scope="function"` on the dependency would not have covered this: the
    function stack is still open while `_complete_chat` awaits the backend, so a
    slow non-streaming generation pins a connection just as hard as a stream
    does. Releasing after the last read covers both.
    """
    seen: list[int] = []

    def record(_request):
        seen.append(pool().checkedout())
        return httpx.Response(200, json=NON_STREAM_REPLY)

    respx.post(UPSTREAM_CHAT).mock(side_effect=record)

    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert seen == [0], f"connection held during the upstream call: {seen}"


@respx.mock
def test_the_pool_does_not_leak_across_many_streams(client, member_key):
    """Releasing early must not mean releasing twice, or never.

    A `commit()` that left the session unusable, or a double release, would show
    up as a drifting checkout count rather than as a failure on any one request.
    """
    respx.post(UPSTREAM_CHAT).mock(return_value=sse())

    for _ in range(8):
        with stream_body(client, member_key) as response:
            "".join(response.iter_text())
        assert pool().checkedout() == 0

    assert pool().checkedout() == 0


# ---------------------------------------------------------------------------
# Nothing after the release needed the request session
# ---------------------------------------------------------------------------
class FakeRedisPipeline:
    def __init__(self, data: dict) -> None:
        self._data = data
        self._ops: list = []

    def hincrby(self, key: str, field: str, value: int) -> None:
        self._ops.append((key, field, value))

    def expire(self, key: str, ttl: int) -> None:  # noqa: ARG002 - TTL is not modelled
        pass

    async def execute(self) -> None:
        for key, field, value in self._ops:
            self._data.setdefault(key, {})
            self._data[key][field] = self._data[key].get(field, 0) + value
        self._ops.clear()


class FakeRedis:
    """Enough of redis.asyncio for `RedisCounterStore`, and not one byte more.

    Bytes in and bytes out, because the gateway builds its client with
    `decode_responses=False` and the store decodes for itself.
    """

    def __init__(self) -> None:
        self.data: dict[str, dict[str, int]] = {}

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self.data)

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {
            field.encode(): str(value).encode()
            for field, value in self.data.get(key, {}).items()
        }

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


def install_store(client, kind: str):
    """Swap the counter store the way `AppState.start` would."""
    from app.core.quota import (
        DatabaseCounterStore,
        QuotaService,
        RedisCounterStore,
    )
    from app.db.session import get_sessionmaker

    state = client.app.state.services
    if kind == "database":
        store = DatabaseCounterStore(get_sessionmaker())
    else:
        store = RedisCounterStore(FakeRedis())
    state.counter_store = store
    state.quota = QuotaService(store, state.registry.snapshot.gateway.quota_defaults)
    return state


def member(client, external_id: str):
    user = client.post(
        "/admin/users",
        json={"external_id": external_id, "display_name": "Streamer", "role": "member"},
        headers=auth(client.admin_key),
    ).json()
    key = client.post(
        "/admin/api-keys",
        json={"user_id": user["id"], "name": "streaming"},
        headers=auth(client.admin_key),
    ).json()
    return user, key["api_key"]


def counted(client, state, user_id: str):
    from app.core.quota import QuotaService

    window = state.registry.snapshot.gateway.quota_defaults.window
    return client.portal.call(
        state.counter_store.get, QuotaService.subject_key(user_id), window
    )


@pytest.mark.parametrize("store_kind", ["database", "redis"])
@respx.mock
def test_quota_is_still_recorded_for_a_stream(client, store_kind):
    """The fix must not quietly depend on which counter store is installed.

    `ctx.finalize` runs in the generator's `finally`, long after the request
    session has gone back to the pool. That is only safe because quota counters
    never used that session: `DatabaseCounterStore` opens its own from the
    sessionmaker, and `RedisCounterStore` has no session at all.
    """
    state = install_store(client, store_kind)
    user, key = member(client, f"649000{len(store_kind)}01")

    respx.post(UPSTREAM_CHAT).mock(return_value=sse())

    before = counted(client, state, user["id"])
    with stream_body(client, key) as response:
        assert response.status_code == 200
        payload = "".join(response.iter_text())
    assert payload.rstrip().endswith("data: [DONE]")

    after = counted(client, state, user["id"])
    assert after.requests == before.requests + 1, store_kind
    # The usage chunk reported 3 in / 2 out; the counters must reflect the
    # stream that actually ran, not a zeroed record written after the release.
    assert after.output_tokens > before.output_tokens, store_kind
    assert (
        after.text_input_tokens + after.visual_input_tokens
        > before.text_input_tokens + before.visual_input_tokens
    ), store_kind


@pytest.mark.parametrize("store_kind", ["database", "redis"])
@respx.mock
def test_the_key_pile_is_still_counted_for_a_stream(client, store_kind):
    """The per-key counter is written from the same `finally` block."""
    from app.core.quota import QuotaService

    state = install_store(client, store_kind)
    user = client.post(
        "/admin/users",
        json={"external_id": f"6491{len(store_kind)}1234", "display_name": "K",
              "role": "member"},
        headers=auth(client.admin_key),
    ).json()
    issued = client.post(
        "/admin/api-keys",
        json={"user_id": user["id"], "name": "ci"},
        headers=auth(client.admin_key),
    ).json()
    client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": issued["id"], "name": "ci",
              "window": "day", "max_requests": 50},
        headers=auth(client.admin_key),
    )

    respx.post(UPSTREAM_CHAT).mock(return_value=sse())
    with stream_body(client, issued["api_key"]) as response:
        "".join(response.iter_text())

    counts = client.portal.call(
        state.counter_store.get, QuotaService.key_subject(issued["id"]), "day"
    )
    assert counts.requests == 1, store_kind


@respx.mock
def test_usage_rows_still_land_after_a_stream(client, member_key):
    """`submit` only buffers; a background task flushes with its own session.

    That is what makes releasing the request session safe here, so it is worth a
    test that the row genuinely reaches the table rather than being dropped on
    the floor by a session that had already gone home.
    """
    from sqlalchemy import func, select

    from app.db.models import UsageLog
    from app.db.session import session_scope

    state = client.app.state.services

    async def count_rows() -> int:
        async with session_scope() as session:
            return await session.scalar(select(func.count()).select_from(UsageLog))

    before = client.portal.call(count_rows)

    respx.post(UPSTREAM_CHAT).mock(return_value=sse())
    with stream_body(client, member_key) as response:
        "".join(response.iter_text())

    # Flush now rather than waiting out the 2 s interval.
    client.portal.call(state.usage.flush)
    assert client.portal.call(count_rows) == before + 1

    async def latest() -> dict:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(UsageLog).order_by(UsageLog.ts.desc()).limit(1)
                )
            ).scalar_one()
            return {
                "stream": row.stream,
                "status": row.status,
                "alias": row.model_alias,
                "output": row.output_tokens,
            }

    row = client.portal.call(latest)
    assert row["stream"] is True
    assert row["status"] == "success"
    assert row["alias"] == "coding"
    assert row["output"] > 0


# ---------------------------------------------------------------------------
# Pool configuration is a decision, not an accident
# ---------------------------------------------------------------------------
def test_sqlite_pool_is_configured_explicitly(client):
    """SQLite used to fall through the `else` and inherit 5+10 silently.

    The ceiling of the whole gateway should not be a number nobody wrote down.
    """
    from app.db import session as session_mod

    engine = session_mod.get_engine()
    assert engine.url.get_backend_name() == "sqlite"
    assert engine.pool.size() == session_mod.SQLITE_POOL_SIZE
    assert engine.pool._max_overflow == session_mod.SQLITE_MAX_OVERFLOW


def test_release_relies_on_expire_on_commit_being_off():
    """Committing mid-request is only safe while loaded objects survive it.

    If someone turns `expire_on_commit` back on, every ORM attribute touched
    after the release fires a lazy refresh and takes a connection straight back
    out - undoing this fix silently, under load, in production only.
    """
    import inspect

    from app.db import session as session_mod

    assert "expire_on_commit=False" in inspect.getsource(session_mod.get_sessionmaker)


def test_every_chat_surface_releases_after_its_last_read():
    """openai, anthropic and responses share this pipeline shape.

    A new surface that resolves limits and forgets to release would reintroduce
    the ceiling for its own callers only, which is the kind of regression that
    surfaces as a mystery 500 months later.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for surface in ("openai", "anthropic", "responses"):
        text = (root / f"app/api/{surface}.py").read_text()
        assert "release_connection(session)" in text, surface
        # After the last read, never before it.
        assert text.index("resolve_key_limits") < text.index(
            "release_connection(session)"
        ), surface


def test_nothing_after_the_release_uses_the_request_session():
    """The guarantee the fix rests on, stated as a test.

    `ctx.finalize` runs minutes later in the generator's `finally`. If a future
    change gives `UsageRecorder.submit` or `QuotaService.record` a session
    parameter, this fix becomes a silent data-loss bug, so the shape of those
    signatures is load-bearing.
    """
    import inspect

    from app.core.quota import DatabaseCounterStore, QuotaService
    from app.core.usage import UsageRecorder

    assert "session" not in inspect.signature(UsageRecorder.submit).parameters
    assert "session" not in inspect.signature(QuotaService.record).parameters
    # The database store carries a sessionmaker, not a session: it opens and
    # closes its own, which is why it still works after the request's has gone.
    assert "session_factory" in inspect.signature(
        DatabaseCounterStore.__init__
    ).parameters


def test_usage_submit_only_buffers(client):
    """Stated directly: submit must not take a connection at all."""
    from app.core.tokens import TokenUsage
    from app.core.usage import build_record

    state = client.app.state.services

    async def submit_without_a_connection() -> int:
        record = build_record(
            request_id="probe",
            principal=None,
            model_alias="coding",
            protocol="openai",
            profile=None,
            usage=TokenUsage(text_input_tokens=1, output_tokens=1),
            endpoint_name="dgx03",
        )
        await state.usage.submit(record)
        return pool().checkedout()

    assert client.portal.call(submit_without_a_connection) == 0
    client.portal.call(state.usage.flush)
