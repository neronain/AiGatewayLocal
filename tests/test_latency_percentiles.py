"""คำขอที่ช้า ช้าแค่ไหน — p50 / p95 / p99 ของ latency และ TTFT ต่อโมเดลและต่อเครื่อง

เดิมรายงานมีแต่ `avg(latency_ms)` · ค่าเฉลี่ยซ่อนสิ่งที่คนมาบ่นพอดี: คำขอ 95 ตัวที่ตอบใน
2 วินาทีกับ 5 ตัวที่ค้าง 90 วินาที เฉลี่ยได้ 6.4 วินาที ซึ่งไม่มีใครเจอเลยสักคน

ทุกเทสในไฟล์นี้เขียนแถว `usage_logs` ผ่าน `UsageRecord.to_row()` กับ session จริง แล้วถาม
`/admin/usage/latency` ตัวจริง · ตัวเลขที่คาดคำนวณด้วยมือและเขียนวิธีคิดไว้ข้าง ๆ

กติกาที่เทสพวกนี้ตรึงไว้ (รายละเอียดอยู่ใน app/core/latency.py):

  * **nearest-rank** — p ของ n ตัวอย่างคือค่าลำดับที่ ceil(p·n/100) จากน้อยไปมาก ·
    ผลเป็นคำขอจริงตัวหนึ่งเสมอ ไม่ใช่ค่าที่เฉลี่ยขึ้นมาระหว่างสองตัว และตรงกับ
    `percentile_disc` ของ PostgreSQL
  * latency นับเฉพาะคำขอที่ **สำเร็จและ backend เป็นคนตอบ** · error · ผู้ใช้ตัดสาย ·
    คำตอบจากแคช ไม่เข้าเปอร์เซ็นไทล์ แต่ถูกนับให้เห็นข้าง ๆ
  * ตัวอย่างน้อยเกินไป = ไม่มีตัวเลข (None) ไม่ใช่ตัวเลขที่ดูน่าเชื่อ
  * ตัดตัวอย่างเมื่อไร ต้องบอกเมื่อนั้น
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from math import ceil

import httpx
import pytest
import respx

from tests.test_api import OPENAI_REPLY, UPSTREAM_CHAT, auth

NOW = datetime.now(timezone.utc)
_ids = itertools.count()


def record(latency_ms: int, **change):
    """แถว usage หนึ่งแถวในรูปที่เกตเวย์เขียนเอง · `age` = กี่วินาทีก่อนตอนนี้"""
    from app.core.usage import UsageRecord

    age = change.pop("age", 60)
    ttft_ms = change.pop("ttft_ms", None)
    fields = {
        "request_id": f"seed-{next(_ids)}",
        "model_alias": change.pop("model", "coding"),
        "protocol": "openai",
        "endpoint_name": change.pop("endpoint", "dgx03"),
        "latency_ms": latency_ms,
        "ttft_ms": ttft_ms,
        "stream": ttft_ms is not None,
        "ts": NOW - timedelta(seconds=age),
        **change,
    }
    return UsageRecord(**fields)


def seed(client, records) -> None:
    from app.db.session import session_scope

    rows = [r.to_row() for r in records]

    async def _write() -> None:
        async with session_scope() as session:
            session.add_all(rows)

    client.portal.call(_write)


def report(client, key: str | None = None, query: str = "days=7") -> dict:
    response = client.get(f"/admin/usage/latency?{query}", headers=auth(key or client.admin_key))
    assert response.status_code == 200, response.text
    return response.json()


def of_model(body: dict, model: str = "coding") -> dict:
    return next(g for g in body["by_model"] if g["model"] == model)


# ---------------------------------------------------------------------------
# ตัวเลข
# ---------------------------------------------------------------------------
def test_the_slow_requests_are_visible_not_averaged_away(client):
    """100 คำขอ 10, 20, … 1000 ms (เขียนลงฐานแบบสลับลำดับ)

    nearest-rank: p50 = ตัวที่ ceil(50) = 50 → 500 · p95 = ตัวที่ 95 → 950 ·
    p99 = ตัวที่ 99 → 990 · ถ้าเป็น linear interpolation จะได้ 505 / 950.5 / 990.1
    """
    latencies = [((i * 37) % 100 + 1) * 10 for i in range(100)]  # 37 กับ 100 ไม่มีตัวหารร่วม
    assert sorted(latencies) == list(range(10, 1001, 10))
    seed(client, [record(ms, age=60 + i) for i, ms in enumerate(latencies)])

    body = report(client)
    assert body["method"] == "nearest-rank"
    assert of_model(body)["latency"] == {
        "population": 100, "samples": 100, "capped": False, "sampled_since": None,
        "p50_ms": 500, "p95_ms": 950, "p99_ms": 990, "max_ms": 1000,
    }


def test_ttft_is_measured_on_streams_only(client):
    """40 stream ที่ TTFT 5, 10, … 200 ms กับ 60 คำขอที่ไม่ stream (ไม่มี TTFT ให้วัด)

    n = 40: p50 = ตัวที่ ceil(20) = 20 → 100 · p95 = ตัวที่ ceil(38) = 38 → 190 ·
    p99 ต้องมี 100 ตัวอย่าง จึงยังไม่มีตัวเลข
    """
    seed(client, [record(3000, ttft_ms=5 * (i + 1), age=60 + i) for i in range(40)])
    seed(client, [record(3000, age=200 + i) for i in range(60)])

    group = of_model(report(client))
    assert group["ttft"] == {
        "population": 40, "samples": 40, "capped": False, "sampled_since": None,
        "p50_ms": 100, "p95_ms": 190, "p99_ms": None, "max_ms": 200,
    }
    assert group["latency"]["samples"] == 100, "latency นับทั้ง stream และไม่ stream"


def test_the_rank_never_goes_through_floating_point():
    """ceil(p·n/100) ต้องตรงกับเศษส่วนจริงทุก n · `ceil(0.07 * 100)` ได้ 8 ไม่ใช่ 7"""
    from app.core.latency import PERCENTILES, nearest_rank

    for n in range(1, 2001):
        ordered = list(range(1, n + 1))  # ค่า = ลำดับของตัวเอง
        for percent in (*PERCENTILES, 7, 29, 57):
            assert nearest_rank(ordered, percent) == max(1, ceil(Fraction(percent * n, 100)))


# ---------------------------------------------------------------------------
# ตัวอย่างน้อยเกินไป
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("samples", "p50", "p95", "p99"),
    [
        (1, None, None, None),
        (19, None, None, None),
        (20, 10, 19, None),     # ceil(10) = 10 · ceil(19) = 19 — เหลือหนึ่งตัวที่ช้ากว่า p95
        (99, 50, 95, None),     # ceil(49.5) = 50 · ceil(94.05) = 95 · p99 จะเป็นตัวช้าสุด
        (100, 50, 95, 99),      # เหลือหนึ่งตัวที่ช้ากว่า p99 พอดี
    ],
)
def test_too_few_samples_is_no_number_at_all(client, samples, p50, p95, p99):
    """p95 ของ 5 คำขอคือ "คำขอที่ช้าที่สุด" ที่ถูกเรียกชื่อให้ดูเป็นสถิติ

    ขั้นต่ำ: p50 กับ p95 ต้องมี 20 ตัวอย่าง · p99 ต้องมี 100 — จำนวนน้อยที่สุดที่ยังมี
    คำขออย่างน้อยหนึ่งตัวช้ากว่าค่าที่รายงาน · ต่ำกว่านั้นได้ None พร้อมจำนวนตัวอย่างและ
    ค่าช้าสุดที่เห็นจริง (ซึ่งเป็นข้อเท็จจริง ไม่ใช่สถิติ)
    """
    seed(client, [record(i + 1, age=60 + i) for i in range(samples)])  # 1, 2, … n ms

    body = report(client)
    assert body["min_samples"] == {"p50": 20, "p95": 20, "p99": 100}
    assert of_model(body)["latency"] == {
        "population": samples, "samples": samples, "capped": False, "sampled_since": None,
        "p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "max_ms": samples,
    }


def test_no_traffic_is_an_empty_report(client):
    body = report(client)
    assert body["by_model"] == []
    assert body["by_endpoint"] == []


def test_a_model_that_only_failed_is_listed_with_nothing_to_measure(client):
    """โมเดลที่ล้มทุกคำขอต้อง *โผล่* ในรายงาน — หายไปเฉย ๆ คือดูดีกว่าความจริงที่สุด"""
    seed(client, [record(30_000, status="error", http_status=502, error_code="UPSTREAM_ERROR")])

    group = of_model(report(client))
    assert (group["requests"], group["errors"]) == (1, 1)
    assert group["latency"] == {
        "population": 0, "samples": 0, "capped": False, "sampled_since": None,
        "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None,
    }
    assert group["ttft"]["samples"] == 0


# ---------------------------------------------------------------------------
# แถวไหนถูกนับ
# ---------------------------------------------------------------------------
def test_failures_hang_ups_and_cache_hits_do_not_improve_the_numbers(client):
    """20 คำขอจริงที่ 1000 ms · กับ 150 แถวที่ "เร็ว" เพราะไม่ได้ทำงาน

    error ที่ล้มใน 1 ms · ผู้ใช้ที่ตัดสายใน 2 ms · คำตอบจากแคชใน 1 ms — ถ้านับรวม p50 จะ
    เป็น 1 ms และ p95 เป็น 1000 ms ทั้งที่คำขอที่โมเดลตอบจริงทุกตัวใช้ 1000 ms
    """
    seed(client, [record(1000, age=60 + i) for i in range(20)])
    seed(client, [record(1, status="error", http_status=502, error_code="UPSTREAM_ERROR",
                         age=100 + i) for i in range(50)])
    seed(client, [record(2, status="aborted", http_status=499, age=200 + i) for i in range(50)])
    seed(client, [record(1, cache_hit=True, age=300 + i) for i in range(50)])

    group = of_model(report(client))
    assert group["latency"] == {
        "population": 20, "samples": 20, "capped": False, "sampled_since": None,
        "p50_ms": 1000, "p95_ms": 1000, "p99_ms": None, "max_ms": 1000,
    }
    # ไม่นับ ≠ ซ่อน: จำนวนที่ถูกกันออกอยู่ข้าง ๆ ตัวเลขเสมอ
    assert group["requests"] == 170
    assert (group["errors"], group["aborted"], group["cache_hits"]) == (50, 50, 50)


def test_ttft_counts_a_first_token_however_the_stream_ended(client):
    """token แรกมาถึงแล้วและถูกจับเวลาแล้ว — สิ่งที่เกิดหลังจากนั้นไม่เปลี่ยนมัน

    ผู้ใช้ coding agent กด Esc กลางคำตอบเป็นเรื่องปกติ ทิ้ง stream พวกนั้นคือทิ้งตัวอย่างดี ๆ
    ส่วน stream ที่จบโดย *ไม่เคยมี* token แรก (backend ค้างจนหมดเวลา · ผู้ใช้เลิกรอ) ไม่มี
    TTFT ให้ใส่ จึงนับแยกไว้ให้เห็น ไม่ใช่หายไปเงียบ ๆ
    """
    seed(client, [record(5000, ttft_ms=400, age=60 + i) for i in range(10)])
    seed(client, [record(900, ttft_ms=800, status="aborted", http_status=499, age=100 + i)
                  for i in range(10)])
    seed(client, [record(60_000, stream=True, status="error", http_status=504,
                         error_code="UPSTREAM_TIMEOUT", age=200 + i) for i in range(3)])
    seed(client, [record(45_000, stream=True, status="aborted", http_status=499, age=300 + i)
                  for i in range(2)])
    # แคชไม่เคยมี TTFT จริง · ต่อให้มีค่าหลุดมาก็ต้องไม่ถูกนับ
    seed(client, [record(1, ttft_ms=1, cache_hit=True, age=400 + i) for i in range(30)])

    group = of_model(report(client))
    assert group["ttft"] == {
        "population": 20, "samples": 20, "capped": False, "sampled_since": None,
        "p50_ms": 400, "p95_ms": 800, "p99_ms": None, "max_ms": 800,
    }
    assert group["streams_without_first_token"] == 5
    assert group["latency"]["samples"] == 10, "latency ยังนับเฉพาะตัวที่จบสำเร็จ"


def test_a_row_that_does_not_say_is_not_a_cache_hit(client):
    """NULL ในคอลัมน์ cache_hit = แถวที่เขียนโดยรุ่นที่ไม่รู้จักคอลัมน์นี้ (ถอยรุ่นแล้วกลับมา)

    ตัวกรองแบบ `cache_hit = false` จะทิ้งแถวพวกนี้เงียบ ๆ — รายงานต้องนับมันเป็นคำขอปกติ
    """
    from sqlalchemy import func, select, update

    from app.db.models import UsageLog
    from app.db.session import session_scope

    seed(client, [record(1000 + i, age=60 + i) for i in range(20)])

    async def forget() -> int:
        # เขียน NULL ตรง ๆ: ORM เติมค่าตั้งต้นให้เองเมื่อได้ None จึงสร้างแถวแบบนี้ผ่านมันไม่ได้
        async with session_scope() as session:
            await session.execute(update(UsageLog).values(cache_hit=None))
            return (await session.execute(
                select(func.count()).where(UsageLog.cache_hit.is_(None)))).scalar_one()

    assert client.portal.call(forget) == 20

    group = of_model(report(client))
    assert (group["requests"], group["cache_hits"]) == (20, 0)
    assert group["latency"]["samples"] == 20


def test_requests_outside_the_window_are_not_counted(client):
    seed(client, [record(100, age=3600)])
    seed(client, [record(90_000, age=3 * 86400)])

    assert of_model(report(client, query="days=1"))["latency"]["max_ms"] == 100
    assert of_model(report(client, query="days=7"))["latency"]["max_ms"] == 90_000


@respx.mock
def test_a_real_cache_hit_stays_out_of_the_percentiles(client, member_key):
    """ของจริงทั้งเส้น: ถามซ้ำแล้วได้จากแคช — คำขอนั้นถูกนับว่ามี แต่ไม่ถูกนับว่าเร็ว

    (แถวถูกติดป้ายตอนเขียนอย่างไร ดู tests/test_cache_hits_are_marked.py)
    """
    from app.core.responsecache import ResponseCache

    client.app.state.services.response_cache = ResponseCache()
    route = respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    ask = {"model": "coding", "temperature": 0,
           "messages": [{"role": "user", "content": "refactor this function"}]}

    replies = [client.post("/v1/chat/completions", headers=auth(member_key), json=ask)
               for _ in range(2)]
    assert [r.headers["x-litegate-cache"] for r in replies] == ["miss", "hit"]
    assert route.call_count == 1

    group = of_model(report(client))
    assert (group["requests"], group["cache_hits"]) == (2, 1)
    assert group["latency"]["samples"] == 1, "นับเฉพาะคำขอที่ backend ตอบ"


# ---------------------------------------------------------------------------
# งานมีขอบเขต และบอกเมื่อถึงขอบ
# ---------------------------------------------------------------------------
def test_a_truncated_sample_says_it_is_truncated(client, monkeypatch):
    """เพดานตัวอย่างต่อกลุ่ม (ของจริง 10,000 · ที่นี่ย่อเป็น 20 ให้เห็นผลด้วยแถวไม่กี่สิบ)

    50 คำขอ: 30 ตัวเก่าที่ 9000 ms · 20 ตัวใหม่สุดที่ 10, 20, … 200 ms · เพดาน 20 = ดูเฉพาะ
    20 ตัวใหม่สุด → p50 = ตัวที่ 10 → 100 · p95 = ตัวที่ 19 → 190 · และต้องบอกว่าดู 20 จาก 50
    ไม่ใช่ปล่อยให้อ่านว่าเป็นตัวเลขของทั้งช่วง
    """
    from app.core import latency

    monkeypatch.setattr(latency, "SAMPLE_CAP", 20)
    # ตัวเก่าถูกเขียนก่อน: "ใหม่สุด" ต้องมาจากเวลาของคำขอ ไม่ใช่จากลำดับที่แถวอยู่ในตาราง
    seed(client, [record(9000, age=1000 + i) for i in range(30)])
    seed(client, [record(10 * (i + 1), age=60 + i) for i in range(20)])
    seed(client, [record(700, model="muse-local", endpoint="dgx01", age=60 + i)
                  for i in range(20)])

    body = report(client)
    assert body["sample_cap"] == 20
    assert of_model(body)["latency"] == {
        "population": 50, "samples": 20, "capped": True,
        # ตัวเก่าสุดใน 20 ตัวที่นับ — ตัวเลขข้างล่างเป็นของช่วงตั้งแต่เวลานี้ ไม่ใช่ทั้ง 7 วัน
        "sampled_since": (NOW - timedelta(seconds=79)).isoformat(),
        "p50_ms": 100, "p95_ms": 190, "p99_ms": None, "max_ms": 200,
    }
    assert of_model(body, "muse-local")["latency"]["capped"] is False, "ถึงเพดานพอดีไม่ใช่ถูกตัด"


def test_rows_that_arrive_between_the_count_and_the_fetch_do_not_break_the_claim(
        client, monkeypatch):
    """ตัวนับกับตัวอย่างเป็นคนละ query · เกตเวย์ยังรับคำขออยู่ระหว่างสองอันนั้น

    กลุ่มที่ตัวนับบอกว่าไม่ถึงเพดานถูกดึงแบบไม่สั่งลำดับ (เอาทุกแถว ลำดับไม่มีผล และเร็วกว่ามาก)
    ถ้าตอนดึงจริงมีเกินเพดาน สิ่งที่ได้ต้องยังเป็น "ตัวใหม่สุด" และบอกว่าถูกตัด — จำลองด้วยการ
    ส่งตัวนับที่ล้าหลัง (5) ให้กลุ่มที่มีจริง 50 แถว
    """
    from app.core import latency
    from app.db.models import UsageLog
    from app.db.session import session_scope

    monkeypatch.setattr(latency, "SAMPLE_CAP", 20)
    seed(client, [record(9000, age=1000 + i) for i in range(30)])  # เขียนตัวเก่าก่อน
    seed(client, [record(10 * (i + 1), age=60 + i) for i in range(20)])

    async def measure() -> dict:
        async with session_scope() as session:
            return await latency._measure(
                session, [UsageLog.ts >= NOW - timedelta(days=1)],
                [UsageLog.model_alias == "coding"], UsageLog.latency_ms,
                latency.LATENCY_ROWS, population=5)

    assert client.portal.call(measure) == {
        "population": 21, "samples": 20, "capped": True,  # "อย่างน้อย 21" — ไม่ใช่ 20 จาก 20
        "sampled_since": (NOW - timedelta(seconds=79)).isoformat(),
        "p50_ms": 100, "p95_ms": 190, "p99_ms": None, "max_ms": 200,
    }


def test_more_groups_than_the_limit_says_so(client, monkeypatch):
    """จำนวนกลุ่มก็มีเพดาน (หนึ่งกลุ่ม = หนึ่งชุด query) · เก็บกลุ่มที่คำขอมากสุดไว้"""
    from app.core import latency

    monkeypatch.setattr(latency, "GROUP_CAP", 2)
    seed(client, [record(100, model="coding", age=60 + i) for i in range(5)])
    seed(client, [record(100, model="muse-local", endpoint="dgx01", age=60 + i)
                  for i in range(3)])
    seed(client, [record(100, model="gemma-vision", endpoint="dgx02")])

    body = report(client)
    assert [g["model"] for g in body["by_model"]] == ["coding", "muse-local"]
    assert body["by_model_truncated"] is True

    monkeypatch.setattr(latency, "GROUP_CAP", 3)
    assert report(client)["by_model_truncated"] is False


# ---------------------------------------------------------------------------
# ต่อเครื่อง
# ---------------------------------------------------------------------------
def test_each_machine_is_measured_apart_for_each_model(client):
    """เครื่องเดียวที่เสิร์ฟสองโมเดลต้องไม่ถูกปนเป็นตัวเลขเดียว

    dgx03 ตอบ embedding ใน 20 ms และตอบ coding ใน 20 วินาที — ปนกันแล้ว p50 ของ "dgx03"
    จะเป็นเลขที่ไม่มีคำขอไหนเป็นแบบนั้น และโมเดลที่ช้าจะถูกบังด้วยโมเดลที่เร็ว
    """
    seed(client, [record(20_000, age=60 + i) for i in range(20)])
    seed(client, [record(40_000, endpoint="dgx04", age=60 + i) for i in range(20)])
    seed(client, [record(20, model="embed", age=60 + i) for i in range(20)])

    rows = {(g["endpoint"], g["model"]): g["latency"]["p50_ms"]
            for g in report(client)["by_endpoint"]}
    assert rows == {("dgx03", "coding"): 20_000, ("dgx04", "coding"): 40_000,
                    ("dgx03", "embed"): 20}


# ---------------------------------------------------------------------------
# ใครเห็นอะไร — เท่ากับ endpoint ข้างเคียง ไม่กว้างกว่า
# ---------------------------------------------------------------------------
def test_a_member_cannot_read_it(client, member_key):
    assert client.get("/admin/usage/latency").status_code in (401, 403)
    assert client.get("/admin/usage/latency", headers=auth(member_key)).status_code in (401, 403)


@pytest.fixture
def classes(client):
    """สองวิชา · อาจารย์ (manager) อยู่วิชาเดียว · นักศึกษาวิชาละคน"""
    admin = auth(client.admin_key)

    def user(external_id: str, role: str = "member") -> dict:
        return client.post("/admin/users", headers=admin,
                           json={"external_id": external_id, "role": role}).json()

    def workspace(code: str, *members: dict) -> dict:
        ws = client.post("/admin/workspaces", headers=admin,
                         json={"code": code, "name": code}).json()
        for member in members:
            client.post(f"/admin/workspaces/{ws['id']}/join", headers=admin,
                        json={"user_id": member["id"]})
        return ws

    lecturer, mine, theirs = user("lecturer", "manager"), user("student-a"), user("student-b")
    cs101, cs202 = workspace("CS101", lecturer, mine), workspace("CS202", theirs)
    key = client.post("/admin/api-keys", headers=admin,
                      json={"user_id": lecturer["id"], "name": "k"}).json()["api_key"]

    seed(client, [record(1000, user_id=mine["id"], workspace_id=cs101["id"], age=60 + i)
                  for i in range(20)])
    seed(client, [record(90_000, user_id=theirs["id"], workspace_id=cs202["id"], age=60 + i)
                  for i in range(30)])
    return {"key": key, "cs101": cs101["id"], "cs202": cs202["id"]}


def test_a_manager_sees_their_own_people_only(client, classes):
    admin_view = of_model(report(client))
    assert (admin_view["requests"], admin_view["latency"]["max_ms"]) == (50, 90_000)

    manager_view = of_model(report(client, classes["key"]))
    assert manager_view["requests"] == 20
    assert manager_view["latency"]["max_ms"] == 1000, "ต้องไม่เห็นคำขอของวิชาที่ตัวเองไม่ได้ดูแล"


def test_the_fleet_layout_stays_with_administrators(client, classes):
    """ชื่อเครื่องและสุขภาพของเครื่องเป็นของ admin อยู่แล้ว (/admin/models · /v1/health/endpoints)

    manager ได้ `null` ไม่ใช่ `[]` — ลิสต์ว่างอ่านว่า "ไม่มีทราฟฟิก" ซึ่งไม่จริง
    """
    assert report(client, classes["key"])["by_endpoint"] is None
    assert [g["endpoint"] for g in report(client)["by_endpoint"]] == ["dgx03"]


def test_scoping_is_the_neighbours_scoping(client, classes):
    """ถามคำถามเดียวกันกับ /admin/usage/summary ต้องได้คนชุดเดียวกัน — ทุกบทบาท ทุกตัวกรอง"""
    def counted(path: str, key: str, query: str) -> dict:
        response = client.get(f"/admin/usage/{path}?days=7{query}", headers=auth(key))
        assert response.status_code == 200, response.text
        return {g["model"]: g["requests"] for g in response.json()["by_model"]}

    for key in (client.admin_key, classes["key"]):
        for query in ("", f"&workspace_id={classes['cs101']}"):
            assert counted("latency", key, query) == counted("summary", key, query)
    assert counted("latency", client.admin_key, f"&workspace_id={classes['cs202']}") == {
        "coding": 30}

    # วิชาที่ไม่ได้ดูแล: ถูกปฏิเสธด้วยคำตอบเดียวกับที่ summary ให้ ไม่ใช่รายงานว่าง
    foreign = f"?workspace_id={classes['cs202']}"
    refused = client.get("/admin/usage/latency" + foreign, headers=auth(classes["key"]))
    neighbour = client.get("/admin/usage/summary" + foreign, headers=auth(classes["key"]))
    assert refused.status_code == neighbour.status_code == 403
    assert refused.json()["error"]["code"] == neighbour.json()["error"]["code"]


# ---------------------------------------------------------------------------
# ฐานข้อมูลอีกยี่ห้อ
# ---------------------------------------------------------------------------
def test_the_queries_are_valid_on_postgresql_too():
    """รันได้แต่บน SQLite ในสายนี้ — อย่างน้อย SQL ที่ออกไปต้อง compile บน dialect ของ
    PostgreSQL และไม่มีฟังก์ชันที่ฐานใดฐานหนึ่งไม่มี (`percentile_cont` ไม่มีบน SQLite)"""
    from sqlalchemy.dialects import postgresql, sqlite

    from app.core import latency
    from app.db.models import UsageLog

    scope = [UsageLog.ts >= NOW]
    keys = {"endpoint": UsageLog.endpoint_name, "model": UsageLog.model_alias}
    match = [UsageLog.model_alias == "coding"]
    statements = [
        latency.tally_statement(scope, keys),
        latency.sample_statement(scope, match, UsageLog.ttft_ms, latency.TTFT_ROWS),
        latency.sample_statement(scope, match, UsageLog.ttft_ms, latency.TTFT_ROWS,
                                 newest_first=False),
    ]
    for statement in statements:
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            sql = str(statement.compile(dialect=dialect)).lower()
            assert "percentile" not in sql and " over " not in sql
    pg = str(statements[1].compile(dialect=postgresql.dialect()))
    assert "cache_hit IS NOT true" in pg and "stream IS true" in pg, pg
    assert "ORDER BY usage_logs.ts DESC" in pg
    assert "ORDER BY" not in str(statements[2].compile(dialect=postgresql.dialect()))
