"""ลิมิตคำขอต่อนาทีต้องนับตอน "รับเข้า" ไม่ใช่ตอน "ตอบเสร็จ"

ตรวจพบ 2026-10-06: นโยบายกลาง `max_requests_per_minute=2` · backend ตอบช้า 0.4
วินาที · ส่งสิบคำขอพร้อมกัน → 200 ทั้งสิบ แล้วคำขอที่ 11 ได้
`429 Your minute request quota is exhausted (10 of 2)`

`check()` อ่านตัวนับ และ `record()` บวกหลังคำตอบจบ — ทั้งสิบตัวจึงอ่านเจอ 0 ก่อนที่ตัว
ไหนจะจบ · PRD §8.1 (NFR-Q1) ยอมให้โควตารายวัน/รายเดือนเกินได้แบบนี้เพราะการจอง token
ล่วงหน้าทำไม่ได้ แต่ตัวกัน burst ที่ปล่อย burst ผ่านไม่ได้กันอะไรเลย
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy.dialects import postgresql, sqlite

from app.core.errors import GatewayError
from app.core.quota import (
    Consumption,
    DatabaseCounterStore,
    KeyLimits,
    QuotaService,
    RedisCounterStore,
    ResilientCounterStore,
    ResolvedLimits,
)
from app.registry.schema import QuotaDefaults
from tests.test_quota_counter_identity import FakeRedis, counter_rows, freeze

CODING = "http://dgx03:8000/v1/chat/completions"
REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "x",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def policy(client, **limits):
    return client.post("/admin/quota-policies", headers=auth(client.admin_key),
                       json={"scope": "global", "window": "day", **limits})


def ask(client, key):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})


def me(client):
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    return next(u for u in users if u["external_id"] == "6412345678")


def minute_counter(client, subject: str) -> Consumption:
    store = client.app.state.services.counter_store
    return client.portal.call(store.get, subject, "minute")


# ---------------------------------------------------------------------------
# ผ่านทางเดินของคำขอจริง
# ---------------------------------------------------------------------------
def test_a_burst_does_not_walk_through_the_per_minute_limit(client, member_key):
    """เคสที่ตรวจพบ: สิบคำขอพร้อมกันใต้ลิมิต 2 ครั้ง/นาที"""
    policy(client, max_requests_per_minute=2)

    def slow(request):
        time.sleep(0.4)
        return httpx.Response(200, json=REPLY)

    results: list[int] = []

    def fire():
        results.append(ask(client, member_key).status_code)

    with respx.mock:
        respx.post(CODING).mock(side_effect=slow)
        threads = [threading.Thread(target=fire) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        late = ask(client, member_key)

    assert sorted(results) == [200, 200] + [429] * 8
    assert late.status_code == 429
    error = late.json()["error"]
    assert (error["details"]["used"], error["details"]["limit"]) == (2, 2), error["message"]


def test_the_request_is_counted_before_the_backend_answers(client, member_key):
    """stream ยาวสิบนาทีต้องถูกนับในนาทีที่มันเริ่มกินเครื่อง ไม่ใช่นาทีที่มันจบ"""
    policy(client, max_requests_per_minute=5)
    subject = f"user:{me(client)['id']}"
    seen: list[int] = []

    store = client.app.state.services.counter_store

    async def answering(request):
        # backend ปลอมอ่านตัวนับ ขณะคำขอยังค้างอยู่กลางทาง
        seen.append((await store.get(subject, "minute")).requests)
        return httpx.Response(200, json=REPLY)

    with respx.mock:
        respx.post(CODING).mock(side_effect=answering)
        assert ask(client, member_key).status_code == 200

    assert seen == [1], "ตอน backend เริ่มทำงาน คำขอนี้ต้องอยู่ในตัวนับนาทีแล้ว"


def test_an_admitted_request_is_counted_once_not_again_when_it_finishes(client, member_key):
    policy(client, max_requests_per_minute=10)
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        for _ in range(3):
            assert ask(client, member_key).status_code == 200

    subject = f"user:{me(client)['id']}"
    minute = minute_counter(client, subject)
    assert minute.requests == 3, "นับตอนรับเข้าแล้ว ต้องไม่นับซ้ำตอนจบ"
    # token ยังลงบัญชีหลังคำตอบจบเหมือนเดิม — ไม่มีใครรู้ตัวเลขก่อนหน้านั้น
    assert (minute.input_tokens, minute.output_tokens) == (30, 15)


def test_a_refused_request_does_not_use_up_the_allowance(client, member_key, monkeypatch):
    """client ที่ยิงซ้ำระหว่างรอต้องไม่ถูกล็อกนานกว่าหน้าต่าง — และตัวนับต้องไม่วิ่งหนี"""
    policy(client, max_requests_per_minute=2)
    now = datetime(2026, 10, 6, 10, 17, 5, tzinfo=timezone.utc)
    freeze(monkeypatch, now)

    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        assert [ask(client, member_key).status_code for _ in range(2)] == [200, 200]
        refused = [ask(client, member_key) for _ in range(6)]
        assert {r.status_code for r in refused} == {429}
        assert refused[-1].json()["error"]["details"]["used"] == 2

        subject = f"user:{me(client)['id']}"
        assert minute_counter(client, subject).requests == 2, "หกครั้งที่ถูกปฏิเสธไม่ได้ถูกนับ"

        # นาทีถัดไป: ได้ครบสองครั้งอีกรอบทันที
        freeze(monkeypatch, now + timedelta(seconds=60))
        assert [ask(client, member_key).status_code for _ in range(3)] == [200, 200, 429]


def test_a_request_stopped_by_another_gate_does_not_use_the_minute(client, member_key):
    """โควตารายวันหมด → 429 · คำขอนั้นไม่ได้ถูกส่งต่อ จึงไม่ควรกินลิมิตต่อนาที"""
    policy(client, max_requests=1, max_requests_per_minute=50)
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        assert ask(client, member_key).status_code == 200
        for _ in range(4):
            refused = ask(client, member_key)
            assert refused.status_code == 429
            assert refused.json()["error"]["details"]["window"] == "day"

    assert minute_counter(client, f"user:{me(client)['id']}").requests == 1


def test_a_token_only_rate_limit_still_admits_and_counts(client, member_key):
    """ตั้งแค่ token ต่อนาที — จำนวนคำขอไม่ถูกจำกัด แต่ยังถูกนับ และด่าน token ยังทำงาน"""
    policy(client, max_tokens_per_minute=40)
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        outcomes = [ask(client, member_key).status_code for _ in range(4)]

    assert outcomes == [200, 200, 200, 429]      # 15 · 30 · 45 → ตัวที่สี่เจอ 45 ≥ 40
    assert minute_counter(client, f"user:{me(client)['id']}").requests == 3


def test_the_daily_counter_still_counts_each_request_once(client, member_key):
    policy(client, max_requests=100, max_requests_per_minute=10)
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        for _ in range(3):
            ask(client, member_key)

    subject = f"user:{me(client)['id']}"
    daily = [n for (name, start), n in counter_rows(client).items()
             if name == subject and start.hour == 0 and start.minute == 0]
    assert daily == [3]


# ---------------------------------------------------------------------------
# ที่เก็บ: การจองต้องเป็นคำสั่งเดียวที่ที่เก็บเป็นคนตัดสิน
# ---------------------------------------------------------------------------
class _RedisDown:
    """Redis ที่ต่อไม่ติด — ทุกคำสั่งโยน"""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise ConnectionError("Connection refused")
        return boom


async def _store(kind: str):
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    database = DatabaseCounterStore(get_sessionmaker())
    if kind == "database":
        return database
    if kind == "redis":
        return RedisCounterStore(FakeRedis())
    if kind == "redis-then-database":
        return ResilientCounterStore(RedisCounterStore(FakeRedis()), database)
    return ResilientCounterStore(RedisCounterStore(_RedisDown()), database)


STORES = ["database", "redis", "redis-then-database", "redis-down"]


@pytest.mark.parametrize("kind", STORES)
async def test_exactly_the_limit_is_admitted_however_many_arrive_together(temp_db, kind):
    store = await _store(kind)

    # 8 ไม่ใช่ 60: SQLite ให้เขียนทีละคน การแย่งกันหนัก ๆ ทดสอบความเร็วดิสก์ ไม่ใช่ตรรกะ
    # (เหตุผลเดียวกับ test_quota_counter_atomicity) · บน PostgreSQL ชุดเดียวกันนี้คือการ
    # เขียนพร้อมกันจริง
    admitted = await asyncio.gather(*[store.reserve("user:u1", "minute", 3) for _ in range(8)])

    assert sorted(admitted) == [False] * 5 + [True] * 3
    assert (await store.get("user:u1", "minute")).requests == 3, "ตัวที่ถูกปฏิเสธต้องไม่ค้างในตัวนับ"


@pytest.mark.parametrize("kind", STORES)
async def test_a_released_place_can_be_taken_again(temp_db, kind):
    store = await _store(kind)
    assert await store.reserve("user:u1", "minute", 1) is True
    assert await store.reserve("user:u1", "minute", 1) is False

    await store.release("user:u1", "minute")

    assert (await store.get("user:u1", "minute")).requests == 0
    assert await store.reserve("user:u1", "minute", 1) is True


@pytest.mark.parametrize("kind", STORES)
async def test_no_limit_means_count_and_admit(temp_db, kind):
    store = await _store(kind)
    assert all([await store.reserve("user:u1", "minute", 0) for _ in range(5)])
    assert (await store.get("user:u1", "minute")).requests == 5


async def test_a_redis_reservation_expires_with_its_minute():
    """คำขอที่ถูกรับแต่ไม่เคยจบ (worker ตาย) ต้องไม่ทิ้งคีย์ค้างใน Redis ตลอดกาล"""
    redis = FakeRedis()
    await RedisCounterStore(redis).reserve("user:u1", "minute", 5)
    (key,) = redis.data
    assert 1 <= redis.ttl[key] <= 60


def test_the_reservation_is_one_conditional_update_on_both_dialects():
    """โครงสร้างของ SQL ไม่ใช่แค่ผลลัพธ์ — เงื่อนไขกับการบวกต้องอยู่ในคำสั่งเดียว

    เทส "มาพร้อมกันแปดตัว" ข้างบนผ่านได้ด้วย SELECT แล้วค่อย UPDATE เหมือนกัน ถ้าจังหวะ
    ของ event loop บังเอิญไม่ชน · สิ่งที่ทำให้มันถูกบน PostgreSQL หลาย worker คือรูปนี้
    """
    statement = DatabaseCounterStore._take_one_statement(
        "user:u1", datetime(2026, 10, 6, 10, 17, tzinfo=timezone.utc), 2
    )
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        flat = " ".join(str(statement.compile(dialect=dialect)).split()).lower()
        assert flat.startswith("update quota_counters set requests="), (dialect.name, flat)
        head, _, where = flat.partition(" where ")
        assert "coalesce(quota_counters.requests" in head, (dialect.name, flat)
        assert "coalesce(quota_counters.requests" in where and " < " in where, (dialect.name, flat)


# ---------------------------------------------------------------------------
# สองด่าน: ของคน แล้วของ key
# ---------------------------------------------------------------------------
def _limits(subject: str, per_minute: int, source: str = "global") -> ResolvedLimits:
    return ResolvedLimits(
        window="day", max_requests=0, max_input_tokens=0, max_output_tokens=0,
        max_images=0, source=source, max_requests_per_minute=per_minute, subject=subject,
    )


@pytest.mark.parametrize("kind", STORES)
async def test_a_key_that_is_full_hands_the_owners_place_back(temp_db, kind):
    """key ชนลิมิตต่อนาทีของตัวเอง → ที่ที่จองไว้ในลิมิตของเจ้าของต้องถูกคืน

    ไม่คืน = key ใบที่ติดเพดานกินลิมิตต่อนาทีของเจ้าของไปทุกครั้งที่ลองใหม่ แล้ว key ใบอื่น
    ของคนเดียวกันโดน 429 ไปด้วยทั้งที่ไม่ได้ทำอะไร
    """
    store = await _store(kind)
    quota = QuotaService(store, QuotaDefaults())
    person = _limits("user:u1", 5)
    key = KeyLimits(subject="key:k1", ceilings=[_limits("key:k1", 1, "key")])

    charge = await quota.admit(person, key)
    assert charge.minutes == ("user:u1", "key:k1")

    for _ in range(3):
        with pytest.raises(GatewayError) as refused:
            await quota.admit(person, key)
        assert refused.value.details["subject"] == "key"
        assert refused.value.details["window"] == "minute"

    assert (await store.get("user:u1", "minute")).requests == 1
    assert (await store.get("key:k1", "minute")).requests == 1


async def test_the_tightest_per_minute_ceiling_on_a_key_is_the_one_reserved_against(temp_db):
    store = await _store("database")
    quota = QuotaService(store, QuotaDefaults())
    person = _limits("user:u1", 0)
    key = KeyLimits(subject="key:k1", ceilings=[
        _limits("key:k1", 0, "key"), _limits("key:k1", 7, "key"), _limits("key:k1", 2, "key"),
    ])

    await quota.admit(person, key)
    await quota.admit(person, key)
    with pytest.raises(GatewayError):
        await quota.admit(person, key)


async def test_no_rate_limit_means_no_extra_write_at_admission(temp_db):
    """deployment ที่ไม่ได้ตั้งลิมิตต่อนาทีต้องไม่จ่ายอะไรเพิ่มต่อคำขอ"""
    store = await _store("database")
    quota = QuotaService(store, QuotaDefaults())

    charge = await quota.admit(_limits("user:u1", 0))

    assert charge.minutes == ()
    assert (await store.get("user:u1", "minute")) == Consumption()


# ---------------------------------------------------------------------------
# หลังรับเข้าแล้ว: ช่องของ backend เต็ม · สลับเครื่อง · ผู้เรียกตัดสาย
# ---------------------------------------------------------------------------
# คำขอถูกนับเข้าตัวนับนาทีตอนรับเข้า ก่อนจะรู้ว่ามีเครื่องรับได้จริงไหม — สิ่งที่เกิดหลังจากนั้น
# ต้องไม่ทำให้มันถูกนับผิด: ถูกปฏิเสธที่ด่านช่อง (ไม่มี backend ไหนได้เห็น) ต้องคืนที่ ·
# สลับเครื่องกลางทางยังเป็นคำขอเดียว
@pytest.mark.parametrize("surface", ["chat", "messages", "responses"])
@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
def test_a_request_refused_at_the_backend_slot_gate_gives_its_minute_back(
        client, member_key, surface, stream):
    """ช่องเต็มทุกเครื่อง → 429 CONCURRENCY_LIMIT_EXCEEDED พร้อม Retry-After: 5 · คนที่ลองใหม่
    ตามนั้นต้องไม่หมดลิมิตต่อนาทีไปกับคำขอที่ไม่เคยได้ทำงาน"""
    from tests.realistic_backends import VllmLike, another_worker_holds, request_for

    policy(client, max_requests_per_minute=2)
    another_worker_holds(client, "coding")
    path, body = request_for(surface, stream=stream)
    subject = f"user:{me(client)['id']}"

    with respx.mock:
        backend = respx.post(CODING).mock(side_effect=VllmLike())
        for _ in range(4):
            refused = client.post(path, headers=auth(member_key), json=body)
            assert refused.status_code == 429, refused.text
            assert "CONCURRENCY_LIMIT_EXCEEDED" in refused.text, (
                "ต้องเป็น 429 ของช่อง backend ทุกครั้ง — ไม่ใช่กลายเป็น 429 ของลิมิตต่อนาที")
        assert not backend.called

    assert minute_counter(client, subject).requests == 0


def test_the_place_is_taken_at_admission_and_handed_back_when_the_slot_gate_refuses(
        client, member_key, monkeypatch):
    """ไม่ใช่ "ไม่เคยถูกนับ": ถูกนับตอนรับเข้าจริง แล้วถูกคืนเมื่อ finalize บอกว่าไม่คิดโควตา
    (`charge=False` จาก lifecycle.take_slot — ทางเดียวกับที่ /v1/embeddings และ /v1/rerank ใช้)"""
    from tests.realistic_backends import another_worker_holds

    policy(client, max_requests_per_minute=2)
    another_worker_holds(client, "coding")
    subject = f"user:{me(client)['id']}"
    services = client.app.state.services
    taken: list[int] = []
    real_release = services.quota.release

    async def watching(charge):
        taken.append((await services.counter_store.get(subject, "minute")).requests)
        await real_release(charge)

    monkeypatch.setattr(services.quota, "release", watching)
    for _ in range(3):
        assert ask(client, member_key).status_code == 429

    assert taken == [1, 1, 1], "แต่ละคำขอถูกนับตอนรับเข้า แล้วถูกคืนเมื่อช่องเต็ม"
    assert minute_counter(client, subject).requests == 0


@pytest.fixture
def two_machines(writable_config):
    """coding มีเครื่องที่สอง — ต้องอยู่ใน config ก่อนแอปเริ่ม จึงต้องมาก่อน `client`"""
    from tests.realistic_backends import add_spare

    add_spare(writable_config)
    return writable_config


def test_a_request_that_fails_over_is_one_request(two_machines, client, member_key):
    """เครื่องแรกล้ม เครื่องที่สองตอบ — ผู้เรียกส่งคำขอเดียว ต้องถูกนับคำขอเดียว"""
    from tests.realistic_backends import SPARE, VllmLike

    policy(client, max_requests=100, max_requests_per_minute=10)
    subject = f"user:{me(client)['id']}"

    with respx.mock:
        first = respx.post(CODING).mock(side_effect=httpx.ConnectError("refused"))
        second = respx.post(f"{SPARE}/v1/chat/completions").mock(side_effect=VllmLike())
        response = ask(client, member_key)

    assert first.called and second.called, "เทสนี้ต้องเดินผ่านสองเครื่องจริง"
    assert response.status_code == 200, response.text
    assert minute_counter(client, subject).requests == 1
    daily = [n for (name, start), n in counter_rows(client).items()
             if name == subject and start.hour == 0 and start.minute == 0]
    assert daily == [1]


def test_a_backend_that_fails_still_counts_as_the_request_it_was(client, member_key):
    """ต่างจากช่องเต็ม: คำขอนี้ไปถึง backend แล้ว — ล้มก็ยังเป็นหนึ่งคำขอของนาทีนั้น"""
    policy(client, max_requests_per_minute=5)
    with respx.mock:
        respx.post(CODING).mock(side_effect=httpx.ConnectError("refused"))
        assert ask(client, member_key).status_code >= 500

    assert minute_counter(client, f"user:{me(client)['id']}").requests == 1


async def test_releasing_a_charge_hands_back_every_minute_it_took(temp_db):
    store = await _store("database")
    quota = QuotaService(store, QuotaDefaults())
    person = _limits("user:u1", 5)
    key = KeyLimits(subject="key:k1", ceilings=[_limits("key:k1", 5, "key")])

    charge = await quota.admit(person, key)
    await quota.release(charge)
    await quota.release(None)          # ทางเข้าที่ไม่มี Charge ต้องไม่พัง

    assert (await store.get("user:u1", "minute")).requests == 0
    assert (await store.get("key:k1", "minute")).requests == 0
