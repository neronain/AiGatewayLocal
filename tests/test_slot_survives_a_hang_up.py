"""ผู้ใช้กด Esc กลาง stream แล้วช่องของ backend ต้องกลับมา — ทั้งคำใบ้ในเครื่องและใบจองร่วม

ตรวจพบ 2026-10-06 กับ redis-server จริง: `Router.release()` pop `_held` แล้ว
`await limiter.release()` ซึ่งเป็น I/O ไป Redis · Starlette ยกเลิก task ของ stream ผ่าน
cancel scope ของ anyio เมื่อผู้เรียกตัดสาย `await` นั้นจึงโยน CancelledError และบรรทัดที่ลด
`_in_flight[slot]` ไม่เคยรัน — ไม่มีอะไรคืนได้อีกเพราะ `_held` ว่างไปแล้ว

    local in_flight hint=1 | limiter count=0
    429 CONCURRENCY_LIMIT_EXCEEDED … at capacity      (ทุกคำขอถัดไปของโมเดล 1 ช่อง)

Esc ครั้งเดียวใน Claude Code = โมเดล llama.cpp 1 ช่องหายไปจาก worker นั้นจน restart · และถ้า
ZREM ยังไม่ทันออกจากเครื่อง ใบจองก็ค้างใน Redis ครบ 15 นาทีให้ทุก worker เห็น

เทสเดิมไม่เคยเจอเพราะ `TestClient` อ่าน stream จนจบเสมอ และตัวนับในเครื่องไม่มี `await`
ที่แตะเครือข่าย · ที่นี่ขับแอปแบบ uvicorn (`receive()` คืน `http.disconnect`) กับตัวนับที่
ต่อ Redis จริง
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time

import httpx
import pytest
import respx

from tests.realistic_backends import (
    CODING,
    MUSE,
    ROLE,
    auth,
    chunk,
    hang_up_on,
    slot_state,
    sse,
    streaming,
    usage_rows,
)

REPLY = {"id": "c", "object": "chat.completion", "model": "up",
         "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": "ok"}}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def _endless(request: httpx.Request) -> httpx.Response:
    """โมเดลที่ยังเขียนอยู่ตอนผู้เรียกตัดสาย — token ละ 20 ms ไปอีก 10 วินาที"""
    import json

    if not json.loads(request.content).get("stream"):
        return httpx.Response(200, json=REPLY)
    steps: list = [ROLE]
    for _ in range(500):
        steps += [sse(chunk({"content": "tok "})), 0.02]
    return streaming(*steps)


class AwaitingRedis:
    """ZSET แบบที่ app/core/inflight.py ใช้ โดยทุกคำสั่งข้าม `await` เหมือน socket จริง"""

    def __init__(self) -> None:
        self.z: dict[str, dict[str, float]] = {}

    def register_script(self, _lua):  # noqa: ANN001
        async def run(keys, args):  # noqa: ANN001
            await asyncio.sleep(0)
            key, (now, ttl, limit, lease) = keys[0], args
            live = {k: v for k, v in self.z.get(key, {}).items() if v > now}
            self.z[key] = live
            if len(live) >= limit:
                return 0
            live[lease] = now + ttl
            return 1
        return run

    async def zrem(self, key, lease):  # noqa: ANN001
        await asyncio.sleep(0.01)  # เที่ยวไป-กลับของเครือข่าย
        self.z.get(key, {}).pop(lease, None)

    async def zremrangebyscore(self, key, _lo, hi):  # noqa: ANN001
        await asyncio.sleep(0)
        self.z[key] = {k: v for k, v in self.z.get(key, {}).items() if v > hi}

    async def zcard(self, key):  # noqa: ANN001
        await asyncio.sleep(0)
        return len(self.z.get(key, {}))


def _share_limiter(client, redis) -> None:  # noqa: ANN001
    """ติดตั้งตัวนับแบบเดียวกับที่ app/state.py ติดเมื่อมี GW_REDIS_URL"""
    from app.core.inflight import (
        LocalInFlightLimiter,
        RedisInFlightLimiter,
        ResilientInFlightLimiter,
    )

    client.app.state.services.router.set_limiter(
        ResilientInFlightLimiter(RedisInFlightLimiter(redis), LocalInFlightLimiter())
    )


@pytest.fixture
def private_redis(tmp_path):
    """redis-server ของเทสนี้เองบนพอร์ตสุ่มของ 127.0.0.1 — ข้ามถ้าเครื่องไม่มี"""
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("ไม่มี redis-server บนเครื่องนี้")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "",
         "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.skip("redis-server ไม่ขึ้น")
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


@respx.mock
def test_esc_in_the_middle_of_a_stream_returns_the_only_slot_with_a_real_redis(
    client, member_key, private_redis
):
    """รูปที่ production รันอยู่: โมเดล 1 ช่อง · ตัวนับร่วมผ่าน Redis · ผู้เรียกกด Esc"""
    import redis.asyncio as aioredis

    respx.post(f"{MUSE}/v1/chat/completions").mock(side_effect=_endless)

    async def connect():
        handle = aioredis.from_url(private_redis, decode_responses=False)
        await handle.ping()
        return handle

    redis = client.portal.call(connect)
    try:
        _share_limiter(client, redis)
        assert slot_state(client, "muse-local") == (0, 0)

        seen = hang_up_on(client, member_key, "chat", "muse-local")
        assert seen["status"] == 200 and seen["frames"] >= 1, seen

        assert slot_state(client, "muse-local") == (0, 0), (
            "คำใบ้ในเครื่องและใบจองใน Redis ต้องกลับเป็น 0 ทั้งคู่หลังผู้เรียกตัดสาย"
        )

        async def leases():
            services = client.app.state.services
            endpoint = services.registry.snapshot.models["muse-local"].spec.endpoints[0]
            return await redis.zrange(
                "inflight:" + services.router._slot("muse-local", endpoint), 0, -1
            )

        assert client.portal.call(leases) == []

        for _ in range(2):  # โมเดล 1 ช่องต้องรับคำขอถัดไปได้ ไม่ใช่ 429 จน restart
            following = client.post(
                "/v1/chat/completions", headers=auth(member_key),
                json={"model": "muse-local", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert following.status_code == 200, following.text
    finally:
        client.portal.call(redis.aclose)


@respx.mock
@pytest.mark.parametrize("surface", ["chat", "messages", "responses"])
def test_hang_ups_do_not_pile_up_on_any_surface(client, member_key, surface):
    """วงจรคืนช่องมีสามสำเนา (สาม surface) — รั่วครบทุกตัว · สามครั้งติดกันต้องไม่ค้างสักช่อง"""
    redis = AwaitingRedis()
    _share_limiter(client, redis)
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=_endless)

    for _ in range(3):
        seen = hang_up_on(client, member_key, surface)
        assert seen["status"] == 200 and seen["frames"] >= 1, seen

    assert slot_state(client, "coding") == (0, 0)
    assert all(not leases for leases in redis.z.values()), redis.z
    row = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"][
        "coding:dgx03"]
    assert row["in_flight"] == 0, "คอนโซลต้องไม่รายงานคำขอที่ไม่มีอยู่จริง"
    # และการตัดสายยังถูกบันทึก — finalize ของแถว usage ต้องรอดการยกเลิกเช่นกัน
    assert len(usage_rows(client)) == 3


@pytest.mark.anyio
async def test_release_finishes_its_local_bookkeeping_before_touching_the_network():
    """หัวใจของบั๊ก: ถูกยกเลิกระหว่างรอ Redis แล้วตัวนับในเครื่องต้องลดไปแล้ว และ ZREM ต้องไปถึง"""
    from app.core.inflight import InFlightLimiter
    from app.core.routing import Router
    from app.registry.schema import Endpoint

    reached_redis = asyncio.Event()
    zrem_done = asyncio.Event()

    class SlowRemote(InFlightLimiter):
        async def acquire(self, key, limit, lease):  # noqa: ANN001
            return True

        async def release(self, key, lease):  # noqa: ANN001
            reached_redis.set()
            await asyncio.sleep(0.05)
            zrem_done.set()

        async def count(self, key):  # noqa: ANN001
            return 0

    class _Registry:
        class snapshot:  # noqa: N801
            models: dict = {}

    router = Router(_Registry())
    router.set_limiter(SlowRemote())
    endpoint = Endpoint(name="only", server_type="llama.cpp", base_url="http://x:1",
                        max_concurrency=1)
    await router.acquire("m", endpoint, "lease-1")
    assert router.in_flight("m", endpoint) == 1

    releasing = asyncio.ensure_future(router.release("m", endpoint, "lease-1"))
    await reached_redis.wait()
    releasing.cancel()                       # ผู้เรียกตัดสายระหว่างที่ ZREM ยังไม่กลับ
    with pytest.raises(asyncio.CancelledError):
        await releasing

    assert router.in_flight("m", endpoint) == 0, "ตัวนับในเครื่องไม่มีเหตุผลต้องรอเครือข่าย"
    await router.drain_releases()
    assert zrem_done.is_set(), "การคืนใบจองต้องไม่ตายไปกับผู้เรียก"
