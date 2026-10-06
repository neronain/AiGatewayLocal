"""ตัวนับหนึ่งตัวต้องนับสิ่งเดียว: (ใคร · ใต้เป้าหมายอะไร · หน้าต่างชนิดไหน)

สองบั๊กที่ตรวจพบ 2026-10-06 มีรากเดียวกัน — ตัวนับถูกเก็บด้วยคีย์ (`user:<id>`,
เวลาเริ่มหน้าต่าง) ซึ่งไม่มีทั้งชนิดหน้าต่างและเป้าหมายของนโยบาย:

* หน้าต่างสองชนิดที่เริ่มวินาทีเดียวกันได้แถวเดียวกัน · นโยบาย `hour` 100 ครั้ง +
  4 ครั้ง/นาที ที่ 10:00:30 → คำขอที่ 3 โดน "minute request quota is exhausted (4 of 4)"
  หลังส่งจริงสองครั้ง และตัวนับรายชั่วโมงขึ้น 4
* นโยบายที่เล็งโมเดลเดียวถูกวัดกับทุกโมเดล · "coding 2 ครั้งต่อวัน" → เรียก
  gemma-vision สองครั้ง แล้ว coding ครั้งแรกในชีวิตได้ 429 "(2 of 2)"

และข้อจำกัดของการแก้: production มีตัวนับที่กำลังนับอยู่ — ตัวนับรายวัน/รายเดือนของคน
ที่ไม่มีนโยบายเจาะจงต้องอยู่ที่เดิม ยอดเดิม
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.quota import (
    WINDOW_KINDS,
    Consumption,
    DatabaseCounterStore,
    QuotaService,
    RedisCounterStore,
    counter_key,
    window_bounds,
)

CODING = "http://dgx03:8000/v1/chat/completions"
VISION = "http://dgx02:8000/v1/chat/completions"
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


@pytest.fixture
def upstream():
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        respx.post(VISION).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def ask(client, key, model="coding"):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def member(client, external_id="6412345678"):
    return next(u for u in admin(client, "GET", "/admin/users").json()["data"]
                if u["external_id"] == external_id)


def counter_rows(client) -> dict[tuple[str, datetime], int]:
    """ทุกแถวใน quota_counters: (ชื่อแถว, เวลาเริ่ม) -> จำนวนคำขอ"""
    from app.db.models import QuotaCounter
    from app.db.session import session_scope

    async def read():
        async with session_scope() as session:
            rows = (await session.execute(select(QuotaCounter))).scalars().all()
            return {(r.subject_key, r.window_start.replace(tzinfo=timezone.utc)): r.requests
                    for r in rows}

    return client.portal.call(read)


def freeze(monkeypatch, moment: datetime) -> None:
    """ให้ app.core.quota เห็นเวลานี้เป็น "ตอนนี้" — หน้าต่างทุกชนิดคิดจากจุดนี้"""
    import app.core.quota as quota_mod

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(quota_mod, "datetime", _Clock)


class FakeRedis:
    """redis.asyncio เท่าที่ RedisCounterStore ใช้ · bytes เข้า bytes ออกเหมือนของจริง
    (เกตเวย์สร้าง client ด้วย decode_responses=False)"""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, int]] = {}
        self.ttl: dict[str, int] = {}

    def pipeline(self):
        return _Pipeline(self)

    async def hincrby(self, key: str, field: str, value: int) -> int:
        row = self.data.setdefault(key, {})
        row[field] = row.get(field, 0) + value
        return row[field]

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttl[key] = ttl
        return key in self.data

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {f.encode(): str(v).encode() for f, v in self.data.get(key, {}).items()}

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class _Pipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list = []

    def hincrby(self, key: str, field: str, value: int) -> None:
        self._ops.append(("hincrby", key, field, value))

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", key, ttl))

    async def execute(self) -> list:
        out = []
        for op, *args in self._ops:
            out.append(await getattr(self._redis, op)(*args))
        self._ops.clear()
        return out


# ---------------------------------------------------------------------------
# Q1 — หน้าต่างต่างชนิดไม่ใช้แถวเดียวกัน
# ---------------------------------------------------------------------------
def test_the_first_minute_of_the_hour_counts_each_request_once(
        client, member_key, upstream, monkeypatch):
    """เคสที่ตรวจพบ: 10:00:30 · นโยบาย hour 100 ครั้ง + 4 ครั้ง/นาที"""
    admin(client, "POST", "/admin/quota-policies", json={
        "scope": "global", "window": "hour", "max_requests": 100,
        "max_requests_per_minute": 4})
    freeze(monkeypatch, datetime(2026, 10, 6, 10, 0, 30, tzinfo=timezone.utc))

    outcomes = [ask(client, member_key).status_code for _ in range(5)]

    assert outcomes == [200, 200, 200, 200, 429], "ตั้งไว้ 4 ครั้งต่อนาที"
    hourly = next(r for r in admin(client, "GET", "/admin/usage/quota").json()["data"]
                  if r["external_id"] == "6412345678")
    assert hourly["used"]["requests"] == 4, "สี่คำขอที่ผ่าน = สี่ ไม่ใช่แปด"


# วินาทีที่หน้าต่างทั้งห้าชนิดเริ่มพร้อมกัน: 1 มิ.ย. 00:00 (มิ.ย. เป็นเดือนต้นเทอมตามค่าเริ่มต้น)
EVERYTHING_STARTS = datetime(2026, 6, 1, 0, 0, 20, tzinfo=timezone.utc)
# วินาทีธรรมดา: ไม่มีหน้าต่างสองชนิดไหนเริ่มพร้อมกัน
ORDINARY = datetime(2026, 10, 6, 10, 17, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("moment", [
    EVERYTHING_STARTS,
    datetime(2026, 10, 1, 0, 0, 5, tzinfo=timezone.utc),     # day = month · hour = day
    datetime(2026, 10, 6, 0, 0, 5, tzinfo=timezone.utc),     # minute = hour = day
    datetime(2026, 10, 6, 10, 0, 30, tzinfo=timezone.utc),   # minute = hour
    ORDINARY,
], ids=["term-start", "first-of-month", "midnight", "top-of-hour", "ordinary"])
def test_no_two_window_kinds_ever_name_the_same_row(moment):
    rows = [(counter_key("user:u1", kind, window_bounds(kind, moment)[0]),
             window_bounds(kind, moment)[0]) for kind in WINDOW_KINDS]
    assert len(set(rows)) == len(WINDOW_KINDS), rows


def test_the_longest_window_starting_at_an_instant_keeps_the_plain_name():
    """ตัวที่ยาวที่สุดได้ชื่อเดิม ตัวที่สั้นกว่าได้ชื่อของตัวเอง — จึงไม่มีสองชนิดได้ชื่อเดียวกัน"""
    first = datetime(2026, 10, 1, tzinfo=timezone.utc)      # ต.ค. ไม่ใช่ต้นเทอม
    assert counter_key("user:u1", "month", first) == "user:u1"
    assert counter_key("user:u1", "day", first) == "user:u1@day"
    assert counter_key("user:u1", "hour", first) == "user:u1@hour"
    assert counter_key("user:u1", "minute", first) == "user:u1@minute"

    june = datetime(2026, 6, 1, tzinfo=timezone.utc)        # มิ.ย. ต้นเทอม
    assert counter_key("user:u1", "term", june) == "user:u1"
    assert counter_key("user:u1", "month", june) == "user:u1@month"


def test_a_window_keeps_one_name_for_its_whole_life():
    """ชื่อขึ้นกับเวลาเริ่มของหน้าต่าง ไม่ใช่ "ตอนนี้" — ไม่งั้นตัวนับจะย้ายแถวกลางเดือน"""
    for day in (1, 2, 15, 31):
        now = datetime(2026, 10, day, 13, 45, tzinfo=timezone.utc)
        assert counter_key("user:u1", "month", window_bounds("month", now)[0]) == "user:u1"


@pytest.mark.parametrize("kind", ["database", "redis"])
async def test_windows_starting_together_are_counted_apart_in_the_store(
        temp_db, monkeypatch, kind):
    """ทั้งสองที่เก็บ — production นับใน Redis และตกมาที่ฐานข้อมูลเมื่อ Redis ล่ม"""
    from app.db.session import get_sessionmaker, init_db

    await init_db()
    store = (DatabaseCounterStore(get_sessionmaker()) if kind == "database"
             else RedisCounterStore(FakeRedis()))
    freeze(monkeypatch, EVERYTHING_STARTS)

    for n, window in enumerate(WINDOW_KINDS, start=1):
        await store.increment("user:u1", window, Consumption(requests=n, output_tokens=10 * n))

    for n, window in enumerate(WINDOW_KINDS, start=1):
        got = await store.get("user:u1", window)
        assert (got.requests, got.output_tokens) == (n, 10 * n), window

    await store.reset("user:u1", "day")
    assert (await store.get("user:u1", "day")).requests == 0
    assert (await store.get("user:u1", "month")).requests == 4, "ล้างรายวันต้องไม่ล้างรายเดือน"


# ---------------------------------------------------------------------------
# ตัวนับที่ production นับอยู่ต้องไม่ขยับ
# ---------------------------------------------------------------------------
async def test_a_counter_written_before_the_upgrade_is_still_read(temp_db, monkeypatch):
    """แถวที่เวอร์ชันก่อนเขียนไว้: ชื่อ `user:<id>` ล้วน ๆ + เวลาเริ่มหน้าต่าง"""
    from app.db.models import QuotaCounter
    from app.db.session import get_sessionmaker, init_db, session_scope

    await init_db()
    freeze(monkeypatch, ORDINARY)
    async with session_scope() as session:
        for window, spent in (("day", 37), ("month", 412), ("term", 2900), ("hour", 5)):
            start, end = window_bounds(window, ORDINARY)
            session.add(QuotaCounter(subject_key="user:somchai", window_start=start,
                                     window_end=end, requests=spent))

    store = DatabaseCounterStore(get_sessionmaker())
    assert (await store.get("user:somchai", "day")).requests == 37
    assert (await store.get("user:somchai", "month")).requests == 412
    assert (await store.get("user:somchai", "term")).requests == 2900
    assert (await store.get("user:somchai", "hour")).requests == 5

    await store.increment("user:somchai", "month", Consumption(requests=1))
    assert (await store.get("user:somchai", "month")).requests == 413, "บวกต่อจากยอดเดิม"


async def test_a_redis_counter_written_before_the_upgrade_is_still_read(monkeypatch):
    """คีย์ Redis ของเวอร์ชันก่อน: `quota:user:<id>:<เวลาเริ่ม ISO>`"""
    freeze(monkeypatch, ORDINARY)
    redis = FakeRedis()
    day_start, _ = window_bounds("day", ORDINARY)
    month_start, _ = window_bounds("month", ORDINARY)
    redis.data[f"quota:user:somchai:{day_start.isoformat()}"] = {"requests": 37}
    redis.data[f"quota:user:somchai:{month_start.isoformat()}"] = {"requests": 412}

    store = RedisCounterStore(redis)
    assert (await store.get("user:somchai", "day")).requests == 37
    assert (await store.get("user:somchai", "month")).requests == 412


def test_a_person_with_no_targeted_policy_is_counted_where_they_always_were(
        client, member_key, upstream):
    me = member(client)
    assert ask(client, member_key).status_code == 200
    assert ask(client, member_key, "gemma-vision").status_code == 200

    day_start, _ = window_bounds("day")
    rows = counter_rows(client)
    assert rows == {(f"user:{me['id']}", day_start): 2}, rows


# ---------------------------------------------------------------------------
# Q2 — นโยบายที่มีเป้าหมายนับเฉพาะการใช้งานใต้เป้าหมายนั้น
# ---------------------------------------------------------------------------
def test_a_limit_on_one_model_is_not_spent_by_calls_to_another(client, member_key, upstream):
    """เคสที่ตรวจพบ: "coding 2 ครั้งต่อวัน" แล้วเรียก gemma-vision สองครั้งก่อน"""
    me = member(client)
    made = admin(client, "POST", "/admin/quota-policies", json={
        "scope": "user", "user_id": me["id"], "model_alias": "coding",
        "window": "day", "max_requests": 2, "name": "2 coding calls a day"})
    assert made.status_code == 201, made.text

    assert ask(client, member_key, "gemma-vision").status_code == 200
    assert ask(client, member_key, "gemma-vision").status_code == 200

    assert ask(client, member_key, "coding").status_code == 200, "coding ยังไม่เคยถูกเรียกเลย"
    assert ask(client, member_key, "coding").status_code == 200
    third = ask(client, member_key, "coding")
    assert third.status_code == 429
    assert third.json()["error"]["details"]["used"] == 2

    # และโมเดลอื่นยังใช้ได้ — เพดานของ coding ไม่ใช่เพดานของคน
    assert ask(client, member_key, "gemma-vision").status_code == 200


def test_usage_under_a_targeted_policy_is_counted_in_its_own_pile(
        client, member_key, upstream):
    """คำขอหนึ่งคำขออยู่ใต้นโยบายเดียว และถูกนับลงกองของนโยบายนั้น"""
    me = member(client)
    admin(client, "POST", "/admin/quota-policies", json={
        "scope": "user", "user_id": me["id"], "model_alias": "coding",
        "window": "day", "max_requests": 50})
    for _ in range(3):
        ask(client, member_key, "coding")
    ask(client, member_key, "gemma-vision")

    day_start, _ = window_bounds("day")
    assert counter_rows(client) == {
        (f"user:{me['id']}:model:coding", day_start): 3,
        (f"user:{me['id']}", day_start): 1,
    }


def test_a_bundle_limit_counts_the_bundle_and_nothing_else(client, member_key, upstream):
    group = admin(client, "POST", "/admin/access-groups",
                  json={"name": "vision-set", "models": ["gemma-vision"]}).json()
    admin(client, "POST", "/admin/quota-policies", json={
        "scope": "global", "access_group_id": group["id"], "window": "day",
        "max_requests": 1})

    for _ in range(3):
        assert ask(client, member_key, "coding").status_code == 200, "coding อยู่นอกมัด"
    assert ask(client, member_key, "gemma-vision").status_code == 200
    assert ask(client, member_key, "gemma-vision").status_code == 429


def test_a_workspace_limit_counts_only_what_was_spent_through_that_workspace(
        client, upstream):
    """key ที่ออกให้วิชาใช้โควตาของวิชา · key ส่วนตัวของคนเดียวกันใช้กองของตัวเอง"""
    person = admin(client, "POST", "/admin/users", json={"external_id": "stu1"}).json()
    ws = admin(client, "POST", "/admin/workspaces",
               json={"code": "CS101", "name": "CS101"}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/models", json={"models": ["coding"]})
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/join", json={"user_id": person["id"]})
    admin(client, "POST", "/admin/quota-policies", json={
        "scope": "workspace", "workspace_id": ws["id"], "window": "day", "max_requests": 2})
    bound = admin(client, "POST", "/admin/api-keys", json={
        "user_id": person["id"], "workspace_id": ws["id"], "name": "bound"}).json()["api_key"]
    loose = admin(client, "POST", "/admin/api-keys", json={
        "user_id": person["id"], "name": "own"}).json()["api_key"]

    for _ in range(3):
        assert ask(client, loose).status_code == 200

    assert ask(client, bound).status_code == 200, "สามครั้งนั้นไม่ได้ใช้ผ่านวิชา"
    assert ask(client, bound).status_code == 200
    assert ask(client, bound).status_code == 429
    assert ask(client, loose).status_code == 200, "โควตาของวิชาหมดไม่ใช่โควตาของคนหมด"


@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/chat/completions", {"stream": True, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages", {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"input": "hi"}),
], ids=["chat", "chat-stream", "messages", "responses"])
def test_every_surface_counts_into_the_pile_of_the_policy_that_bound_it(
        client, member_key, path, body):
    """ทุกทางเข้าต้องนับลงกองเดียวกับที่ถูกตรวจ — /v1/responses เคยตรวจเพดานของ key
    แต่ไม่เคยนับ (2026-10-05) เพราะทางนั้นสร้าง context โดยลืมส่งของไปด้วย

    ไม่มี backend จริงในเทสนี้ คำขอจึงล้มที่ upstream — ซึ่งยังต้องถูกนับ และถูกนับลง
    กองของ coding ไม่ใช่กองรวม
    """
    me = member(client)
    admin(client, "POST", "/admin/quota-policies", json={
        "scope": "user", "user_id": me["id"], "model_alias": "coding",
        "window": "day", "max_requests": 1})
    headers = auth(member_key)

    first = client.post(path, headers=headers, json={"model": "coding", **body})
    assert first.status_code != 429
    second = client.post(path, headers=headers, json={"model": "coding", **body})
    assert second.status_code == 429, second.text

    day_start, _ = window_bounds("day")
    assert counter_rows(client) == {(f"user:{me['id']}:model:coding", day_start): 1}


def test_subject_names():
    """ชื่อกอง — กองที่ไม่เล็งอะไรต้องเป็นชื่อเดิมเป๊ะ เพราะ production นับอยู่ใต้ชื่อนี้"""
    assert QuotaService.subject_key("u1") == "user:u1"
    assert QuotaService.subject_key("u1", "coding") == "user:u1:model:coding"
    assert QuotaService.subject_key("u1", workspace_id="w1") == "user:u1:ws:w1"
    assert QuotaService.subject_key("u1", access_group_id="g1") == "user:u1:group:g1"
    assert (QuotaService.subject_key("u1", "coding", workspace_id="w1")
            == "user:u1:ws:w1:model:coding")
    # ยาวสุดที่เป็นไปได้ต้องลงคอลัมน์ subject_key (160) ได้ รวม `@minute`
    longest = QuotaService.subject_key("a" * 32, "m" * 63, workspace_id="b" * 32)
    assert len(longest + "@minute") <= 160
