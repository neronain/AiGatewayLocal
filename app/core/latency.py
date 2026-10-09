"""เปอร์เซ็นไทล์ของ latency และ TTFT จาก `usage_logs` — ฝั่งอ่านอย่างเดียว

รายงานเดิมมีแต่ `avg(latency_ms)` · ค่าเฉลี่ยซ่อนสิ่งที่คนมาบ่นพอดี: คำขอ 95 ตัวที่ตอบใน
2 วินาทีกับ 5 ตัวที่ค้าง 90 วินาที เฉลี่ยได้ 6.4 วินาที ซึ่งไม่มีใครเจอเลยสักคน · Prometheus มี
histogram อยู่แล้ว (`main.LATENCY` · `main.TTFT`) แต่ลูกค้าที่ใช้คอนโซลอย่างเดียวไม่มี Grafana
และ histogram ไม่รู้ว่าคำขอไหนเป็นของ workspace ไหน — ไฟล์นี้คือทางของคอนโซล ไม่ได้วัดซ้ำ

ไม่มีอะไรในนี้ถูกเรียกจากทางเดินของคำขอ · ตัวเลขทุกตัวมาจากแถวที่ `core/usage.py` เขียนอยู่แล้ว

แถวไหนถูกนับ — ค่าตั้งต้นต้องไม่ทำให้ระบบดูดีกว่าที่เป็น
----------------------------------------------------
* **latency** = คำขอที่ `status = success` และ backend เป็นคนตอบ ทั้ง stream และไม่ stream
  (ทั้งสองแบบวัดสิ่งเดียวกัน: รับคำขอ → ไบต์สุดท้าย) · error ที่ล้มใน 5 ms กับผู้ใช้ที่ตัดสาย
  ในวินาทีที่สามไม่ได้บอกว่า "คำตอบใช้เวลาเท่าไร" นับรวมแล้วมีแต่ทำให้เลขต่ำลง
* **TTFT** = stream ที่ token แรกมาถึง จะจบแบบไหนก็ตาม — มันถูกจับเวลาไปแล้ว และผู้ใช้
  coding agent กด Esc กลางคำตอบเป็นเรื่องปกติ · คำขอที่ไม่ stream ไม่มี TTFT (`ttft_ms` เป็น
  NULL) · stream ที่จบโดยไม่เคยมี token แรก (backend ค้างจนหมดเวลา · ผู้ใช้เลิกรอ) ไม่มีค่า
  ให้ใส่ จึงถูก **นับแยก** ไว้ข้าง ๆ — ไม่งั้นโมเดลที่ค้างหนึ่งในสามจะมี TTFT สวยที่สุด
* **cache hit ไม่ถูกนับเลย** — ตอบใน ~1 ms โดยไม่มีเครื่องไหนทำงาน (ดู `UsageLog.cache_hit`)
* ทุกอย่างที่ถูกกันออกมีจำนวนกำกับในคำตอบ: ไม่นับ ≠ ซ่อน

วิธีคิด: nearest-rank
---------------------
p ของ n ตัวอย่าง = ค่าลำดับที่ ceil(p·n/100) จากน้อยไปมาก · เลือกแบบนี้แทน linear
interpolation เพราะ (1) ผลเป็นคำขอจริงตัวหนึ่งเสมอ ไม่ใช่ค่าที่เฉลี่ยขึ้นมาระหว่างสองตัว
(2) ตัวอย่างน้อยมันไม่มีทางรายงานต่ำกว่าที่เห็น — interpolation ดึง p99 ลงไปหาตัวรองช้าสุด
(3) เป็นนิยามเดียวกับ `percentile_disc` ของ PostgreSQL ใช้เทียบผลกันได้ตรง ๆ

ฐานข้อมูลไม่ได้เป็นคนคิดเปอร์เซ็นไทล์ · SQL ที่ออกไปมีแต่ WHERE / ORDER BY ts / LIMIT กับ
COUNT ซึ่ง SQLite และ PostgreSQL ตอบเหมือนกัน (SQLite ไม่มี `percentile_cont`) แล้วเรียงและ
หยิบลำดับใน Python ด้วยเลขจำนวนเต็มล้วน — คำตอบจึงเท่ากันบนสองฐานโดยโครงสร้าง

งานมีขอบเขต
-----------
ตารางจริงวันนี้มีไม่กี่ร้อยแถว แต่ต้องไม่ล้มเมื่อเป็นล้าน: ดึงมาคิดไม่เกิน `SAMPLE_CAP` ตัวอย่าง
**ใหม่สุด** ต่อกลุ่มต่อมาตรวัด และไม่เกิน `GROUP_CAP` กลุ่ม · ถึงเพดานเมื่อไรคำตอบบอกเมื่อนั้น
(`capped` · `population` · `*_truncated`) — เปอร์เซ็นไทล์จากตัวอย่างที่ถูกตัดต้องไม่ถูกอ่านว่า
เป็นของทั้งช่วง · ตัวนับต่อกลุ่มเป็น aggregate เดียวบนช่วงเวลา (งานเท่ากับที่
`/admin/usage/summary` ทำอยู่แล้ว) ส่วนการดึงตัวอย่างของกลุ่มที่เกินเพดานไล่ index ของ `ts`
จากใหม่ไปเก่าแล้วหยุดที่เพดาน

OrcaRouter-Lite (`latency_by_provider`) เป็นที่มาของแนวคิด แต่ไม่ได้ยกโค้ดมา: ตัวนั้นโหลดทุก
แถวของช่วงเวลาเข้าหน่วยความจำ นับ error รวมกับคำขอสำเร็จ และหยิบลำดับด้วย
`round((n-1)·p)` ซึ่งปัดครึ่งเป็นเลขคู่ — n=4 กับ n=6 จึงได้ p50 คนละนิยาม
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import _aware
from app.db.models import UsageLog

METHOD = "nearest-rank"
PERCENTILES = (50, 95, 99)

# จำนวนตัวอย่างน้อยที่สุดที่ยอมให้มีตัวเลข · 20 และ 100 คือ n น้อยที่สุดที่ยังเหลือคำขอ
# อย่างน้อยหนึ่งตัว *ช้ากว่า* p95 และ p99 (ceil(0.95·20) = 19 · ceil(0.99·100) = 99) —
# ต่ำกว่านั้น "p99" คือคำขอที่ช้าที่สุดตัวเดียวที่ถูกเรียกชื่อให้ดูเป็นสถิติ · p50 ใช้ 20
# ด้วย: มัธยฐานของคำขอไม่กี่ตัวขยับทั้งค่าทุกครั้งที่มีคำขอใหม่ · ต่ำกว่าขั้นต่ำได้ None
# พร้อมจำนวนตัวอย่างและค่าช้าสุดที่เห็นจริง ซึ่งเป็นข้อเท็จจริง ไม่ใช่สถิติ
MIN_SAMPLES = {50: 20, 95: 20, 99: 100}

# ตัวอย่างใหม่สุดต่อกลุ่มต่อมาตรวัด · 10,000 ตัว = 100 คำขอเหนือ p99 ซึ่งนิ่งพอ และเป็น
# จำนวนเต็มไม่กี่หมื่นตัวต่อกลุ่มในหน่วยความจำ ไม่ว่าตารางจะใหญ่แค่ไหน
SAMPLE_CAP = 10_000
# หนึ่งกลุ่ม = หนึ่งชุด query · เก็บกลุ่มที่คำขอมากสุดไว้
GROUP_CAP = 50

# NULL = แถวที่เขียนก่อนมีคอลัมน์ หรือโดยรุ่นที่ไม่รู้จักมัน — นับเป็น "ไม่ใช่แคช"
_NOT_CACHED = UsageLog.cache_hit.is_not(True)
_STREAMED = UsageLog.stream.is_(True)

LATENCY_ROWS = (UsageLog.status == "success", _NOT_CACHED)
TTFT_ROWS = (_STREAMED, UsageLog.ttft_ms.is_not(None), _NOT_CACHED)
NO_FIRST_TOKEN_ROWS = (_STREAMED, UsageLog.ttft_ms.is_(None), _NOT_CACHED)


def nearest_rank(ordered: Sequence[int], percent: int) -> int:
    """ค่าลำดับที่ ceil(percent·n/100) ของลิสต์ที่เรียงแล้วและไม่ว่าง

    จำนวนเต็มล้วน: `ceil(0.07 * 100)` ได้ 8 เพราะ 0.07 เก็บเป็น float ไม่ได้พอดี
    """
    rank = -(-percent * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


def summarise(
    values: Sequence[int], population: int, sampled_since: datetime | None = None
) -> dict[str, Any]:
    """ตัวเลขของมาตรวัดหนึ่งตัวในกลุ่มหนึ่ง · `population` = แถวที่เข้าเกณฑ์ทั้งช่วงเวลา

    `sampled_since` = เวลาของตัวอย่างเก่าสุดเมื่อถูกตัดที่เพดาน · "10,000 ตัวใหม่สุด" ของโมเดลที่
    งานชุกคือไม่กี่ชั่วโมงล่าสุด ไม่ใช่ทั้ง 30 วันที่ขอ — บอกช่วงที่ตัวเลขครอบคลุมจริงไปด้วย
    ตัวเลขจึงเป็นค่าที่ถูกต้องของช่วงเวลาที่สั้นกว่า ไม่ใช่ค่าประมาณของช่วงที่ขอ
    """
    ordered = sorted(values)
    capped = sampled_since is not None
    summary: dict[str, Any] = {
        # ตัวนับกับตัวอย่างมาจากสอง query · แถวที่เข้ามาคั่นกลางต้องไม่ทำให้ได้ "120 จาก 119"
        # หรือ "ตัดแล้ว 20 จาก 20" — ถูกตัดแปลว่ามีมากกว่าที่เอามาอย่างน้อยหนึ่งแถว
        "population": max(population, len(ordered) + capped),
        "samples": len(ordered),
        "capped": capped,
        "sampled_since": _aware(sampled_since).isoformat() if sampled_since else None,
    }
    for percent in PERCENTILES:
        enough = len(ordered) >= MIN_SAMPLES[percent]
        summary[f"p{percent}_ms"] = nearest_rank(ordered, percent) if enough else None
    summary["max_ms"] = ordered[-1] if ordered else None
    return summary


def _count(*conditions: Any):  # noqa: ANN202
    return func.sum(case((and_(*conditions), 1), else_=0))


def tally_statement(scope: Sequence[Any], keys: dict[str, Any]):  # noqa: ANN201
    """ตัวนับต่อกลุ่ม: ทั้งหมด · ที่ถูกกันออกแต่ละแบบ · และที่เข้าเกณฑ์ของแต่ละมาตรวัด

    เกณฑ์ในนี้คือ tuple ตัวเดียวกับที่ `sample_statement` ใช้ดึงตัวอย่าง — "นับได้กี่ตัว" กับ
    "เอาตัวไหนมาคิด" จึงเพี้ยนจากกันไม่ได้
    """
    columns = list(keys.values())
    requests = func.count(UsageLog.id)
    return (
        select(
            *columns,
            requests,
            _count(UsageLog.status == "error"),
            _count(UsageLog.status == "aborted"),
            _count(UsageLog.cache_hit.is_(True)),
            _count(*LATENCY_ROWS),
            _count(*TTFT_ROWS),
            _count(*NO_FIRST_TOKEN_ROWS),
        )
        .where(*scope)
        .group_by(*columns)
        # ชื่อกลุ่มเป็นตัวตัดสินเมื่อเท่ากัน — กลุ่มที่หลุดเพดานต้องเป็นกลุ่มเดิมทุกครั้งที่ถาม
        .order_by(requests.desc(), *columns)
        .limit(GROUP_CAP + 1)
    )


def sample_statement(  # noqa: ANN201
    scope: Sequence[Any],
    match: Sequence[Any],
    column: Any,
    rows: Sequence[Any],
    newest_first: bool = True,
):
    """ตัวอย่างของกลุ่มเดียว · ขอเกินเพดานหนึ่งแถวเพื่อรู้ว่ามีมากกว่านั้นจริงไหม

    `newest_first=False` = ไม่สั่งลำดับ ใช้เมื่อรู้แล้วว่าทั้งกลุ่มไม่ถึงเพดาน (เอามาทุกแถว
    ลำดับจึงไม่มีผล) · มี ORDER BY ts แล้วฐานข้อมูลเลือกไล่ index ของ `ts` ทั้งช่วงเวลาเพื่อ
    กรองหากลุ่มเล็ก ๆ — วัดบน SQLite 1,000,000 แถว (2026-10-09): โมเดลที่มี 954 แถวใน 30 วัน
    ใช้ 1.02 วินาทีเมื่อสั่งลำดับ และ 0.01 วินาทีเมื่อไม่สั่ง (ไปทาง index ของ model_alias)
    """
    statement = select(column, UsageLog.ts).where(*scope, *match, *rows).limit(SAMPLE_CAP + 1)
    if newest_first:
        statement = statement.order_by(UsageLog.ts.desc(), UsageLog.id.desc())
    return statement


async def _measure(  # noqa: PLR0913
    session: AsyncSession,
    scope: Sequence[Any],
    match: Sequence[Any],
    column: Any,
    rows: Sequence[Any],
    population: int,
) -> dict[str, Any]:
    if not population:
        return summarise([], 0)

    async def fetch(newest_first: bool) -> Sequence[Any]:
        statement = sample_statement(scope, match, column, rows, newest_first)
        return (await session.execute(statement)).all()

    found = await fetch(population > SAMPLE_CAP)
    if population <= SAMPLE_CAP < len(found):
        # ตัวนับบอกว่าไม่ถึงเพดาน แต่มีแถวเข้ามาเพิ่มก่อนจะดึง · แถวที่ได้มาแบบไม่สั่งลำดับ
        # ไม่ใช่ "ตัวใหม่สุด" อย่างที่คำตอบจะบอก — ดึงใหม่ให้เป็นอย่างนั้นจริง
        found = await fetch(True)
    kept = found[:SAMPLE_CAP]
    # เรียงจากใหม่ไปเก่า แถวสุดท้ายที่เก็บไว้จึงเป็นตัวเก่าสุดของตัวอย่าง
    cut_at = kept[-1][1] if len(found) > SAMPLE_CAP else None
    return summarise([value for value, _ in kept], population, cut_at)


async def report(
    session: AsyncSession, scope: Sequence[Any], keys: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """เปอร์เซ็นไทล์ต่อกลุ่ม · คืน (กลุ่มเรียงจากคำขอมากไปน้อย, มีกลุ่มที่ถูกตัดทิ้งไหม)

    `scope` = เงื่อนไขที่ผู้เรียกตัดสินแล้ว (ช่วงเวลา · workspace · คนที่มองเห็นได้) ใช้กับทุก
    query เหมือนกัน · `keys` = ชื่อฟิลด์ในคำตอบ → คอลัมน์ที่ใช้แบ่งกลุ่ม
    """
    tallies = (await session.execute(tally_statement(scope, keys))).all()
    groups: list[dict[str, Any]] = []
    for tally in tallies[:GROUP_CAP]:
        values = tally[: len(keys)]
        requests, errors, aborted, cached, timed, first_tokens, silent = (
            int(count or 0) for count in tally[len(keys):]
        )
        match = [column == value for column, value in zip(keys.values(), values, strict=True)]
        groups.append({
            **dict(zip(keys, values, strict=True)),
            "requests": requests,
            "errors": errors,
            "aborted": aborted,
            "cache_hits": cached,
            "latency": await _measure(
                session, scope, match, UsageLog.latency_ms, LATENCY_ROWS, timed),
            "ttft": await _measure(
                session, scope, match, UsageLog.ttft_ms, TTFT_ROWS, first_tokens),
            "streams_without_first_token": silent,
        })
    return groups, len(tallies) > GROUP_CAP
