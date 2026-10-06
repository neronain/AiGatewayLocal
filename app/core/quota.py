"""Quota and rate limiting (PRD §10, FR-20..FR-24).

Two enforcement points per request:

  * `check()`  before forwarding - reads counters, rejects with 429 if a limit
    is already reached.
  * `record()` after completion  - increments counters with actual usage.

This is check-then-record, not reserve-then-settle: under a concurrent burst a
member can overshoot by at most (in-flight requests x per-request cost). That
is accepted deliberately (NFR-Q1) because reserving would require holding a
lock across a multi-minute generation. Overrun self-corrects on the next check.

One limit is the exception: **requests per minute**. A burst limiter that
counts completions lets the whole burst through - every request passes the
check before any of them has finished - so that one count is taken at
admission, atomically (`admit()` -> `CounterStore.reserve`). Tokens are still
settled after the response, because nobody knows them before.

Counters live in Redis when configured (shared across workers) and fall back to
the database otherwise, which is correct for a single-worker deployment.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prometheus_client import Gauge
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
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
    # ตัวนโยบายที่ชนะ — เพื่อให้หน้าจอบอกได้ว่า "ตัวเลขนี้มาจากกฎข้อไหน" ไม่ใช่แค่
    # ระดับของมัน · ผู้ดูแลที่เห็นแค่คำว่า "workspace" ยังต้องไปไล่หาต่ออยู่ดีว่าอันไหน
    policy_id: str = ""
    policy_name: str = ""
    # ตัวนับที่ลิมิตชุดนี้ถูกวัดด้วย · เป้าหมายของนโยบายเป็นส่วนหนึ่งของชื่อ — นโยบายที่
    # เล็งโมเดลเดียววัดกับการใช้โมเดลนั้น ไม่ใช่กับทุกอย่างที่คนคนนี้ใช้ (ดู subject_key)
    subject: str = ""
    # เป้าหมายของนโยบายที่ชนะ — ให้หน้าจอบอกได้ว่ากฎนี้ครอบอะไร
    workspace_id: str | None = None
    model_alias: str | None = None
    access_group_id: str | None = None

    @property
    def rate_limited(self) -> bool:
        return bool(self.max_requests_per_minute or self.max_tokens_per_minute)


@dataclass
class KeyLimits:
    """ทุกเพดานที่ตั้งไว้บน key ใบเดียว — **แต่ละอันถูกบังคับแยกกัน**

    เดิมเลือกมาอันเดียวด้วย `min(..., key=max_requests or 1 << 62)` คือ "อันที่
    max_requests น้อยที่สุด" แล้วทิ้งที่เหลือ · key ที่มีเพดาน 1,000 ครั้ง/วัน กับอีกอัน
    5 output token/วัน จึงถูกบังคับแค่อันแรก — เรียกสี่ครั้ง ครั้งละ 5 output token
    ผ่านหมด (ตรวจพบ 2026-10-06) · เพดานมีไว้เพื่อจำกัด อันที่ถูกทิ้งคืออันที่ไม่ได้จำกัดอะไร
    โดยไม่มีอะไรบอก

    ทุกอันวัดกับกองเดียวกัน (`key:<id>`) ในหน้าต่างของตัวเอง
    """

    subject: str
    ceilings: list[ResolvedLimits]

    @property
    def window(self) -> str:
        return self.ceilings[0].window

    @property
    def windows(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(c.window for c in self.ceilings))

    @property
    def rate_limited(self) -> bool:
        return any(c.rate_limited for c in self.ceilings)

    @property
    def max_requests_per_minute(self) -> int:
        """อันที่เข้มที่สุดในบรรดาที่ตั้งไว้ · 0 = ไม่มีอันไหนจำกัดจำนวนคำขอต่อนาที"""
        set_limits = [c.max_requests_per_minute for c in self.ceilings
                      if c.max_requests_per_minute]
        return min(set_limits) if set_limits else 0


@dataclass(frozen=True)
class Charge:
    """คำขอหนึ่งคำขอถูกนับลงตัวนับไหนบ้าง — ตัดสินตอนรับคำขอ ใช้ตอนคำขอจบ

    `record()` รันหลังคำตอบจบ ซึ่งสำหรับ stream คือหลายนาทีหลัง session ของคำขอถูก
    คืนไปแล้ว จึงถามฐานข้อมูลซ้ำไม่ได้ว่านโยบายไหนชนะ (และไม่ควร: นโยบายอาจถูกแก้
    ระหว่างทาง แล้วคำขอจะถูกตรวจกับกองหนึ่งแต่ไปนับลงอีกกอง) · ใบนี้พกคำตอบไปด้วย
    """

    # (subject, หน้าต่าง) ที่ต้องบวกการใช้งานจริงเมื่อคำขอจบ
    windows: tuple[tuple[str, str], ...] = ()
    # subject ที่มีลิมิตต่อนาที · **คำขอถูกนับเข้าตัวนับนาทีไปแล้วตอนรับเข้า** — เมื่อจบ
    # จึงบวกเฉพาะ token ถ้าบวกคำขออีกรอบคือนับสองครั้ง
    minutes: tuple[str, ...] = ()


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
    now = now or datetime.now(timezone.utc)
    # A per-minute window rides the same counters as the daily one. A day's
    # quota stops somebody using a term's worth in a week; it does nothing about
    # forty people pressing send at the start of a class, which is the shape of
    # the load this actually gets.
    if window == "minute":
        start = now.replace(second=0, microsecond=0)
        return start, start + timedelta(minutes=1)
    # หน้าต่างรายชั่วโมง — อยู่ระหว่างนาทีกับวัน
    #
    # โควตารายวันหยุดคนที่ใช้เกินตัวได้ก็จริง แต่คนที่เผลอปล่อย loop ตอนเช้าจะโดนตัด
    # ไปทั้งวัน · ลิมิตต่อนาทีก็สั้นเกินจะเป็นเพดานของงานจริง · รายชั่วโมงคือช่วงที่
    # "พลาดแล้วรอไม่นานเกินไป" ซึ่งเป็นสิ่งที่ token ของสคริปต์ต้องการจริง ๆ
    if window == "hour":
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if window == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = monthrange(now.year, now.month)[1]
        return start, start + timedelta(days=last_day)
    if window == "term":
        return _term_bounds(now, term_start_months or DEFAULT_TERM_START_MONTHS)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


# สั้นไปยาว · ลำดับนี้คือสิ่งที่ counter_key ใช้ตัดสินว่า "หน้าต่างที่ยาวกว่า" คืออะไร
WINDOW_KINDS = ("minute", "hour", "day", "month", "term")


def counter_key(subject: str, window: str, start: datetime) -> str:
    """ชื่อแถวของตัวนับ (subject, ชนิดหน้าต่าง) ที่เริ่ม ณ `start`

    ตัวนับถูกเก็บด้วยคีย์ (ชื่อ, เวลาเริ่มหน้าต่าง) — ทั้งแถวใน `quota_counters`
    (`uq_counter_window`) และคีย์ของ Redis (`quota:<ชื่อ>:<เวลาเริ่ม>`) · **ชนิดของ
    หน้าต่างไม่ได้อยู่ในคีย์** หน้าต่างสองชนิดที่เริ่มวินาทีเดียวกันจึงเป็นแถวเดียวกัน:

      * นาทีแรกของชั่วโมง — ตัวนับ "นาที" กับตัวนับ "ชั่วโมง" คือแถวเดียว · record()
        บวกคำขอเข้าตัวนับของหน้าต่างแล้วบวกเข้าตัวนับนาทีอีกรอบ = **นับสองครั้ง** และ
        ด่านต่อนาทีอ่านยอดของทั้งชั่วโมง (ตรวจพบ 2026-10-06: นโยบาย hour 100 ครั้ง +
        4 ครั้ง/นาที ที่ 10:00:30 → คำขอที่ 3 โดน "minute request quota is exhausted
        (4 of 4)" ทั้งที่ส่งจริงสองครั้ง)
      * ชั่วโมงแรกของวัน (hour/day) · วันที่ 1 (day/month) · เดือนที่เทอมเริ่ม (month/term)

    ทางแก้ต้องไม่ย้ายตัวนับที่ production นับอยู่: คีย์เดิมไม่มีชนิดหน้าต่าง ถ้าเติมชนิดให้
    ทุกตัว ยอดรายวัน/รายเดือนของทุกคนจะกลับเป็นศูนย์ในวันที่ deploy

    จึงเติม `@<ชนิด>` **เฉพาะเมื่อมีหน้าต่างที่ยาวกว่าเริ่มวินาทีเดียวกัน** · ตัวที่ยาวที่สุด
    ณ วินาทีนั้นได้ชื่อเดิมไป ตัวที่สั้นกว่าทุกตัวได้ชื่อของตัวเอง — สองชนิดจึงไม่มีทางได้
    ชื่อเดียวกัน และแถวเกือบทั้งหมด (วันที่ไม่ใช่วันที่ 1 · เดือนที่ไม่ใช่ต้นเทอม · นาทีที่
    ไม่ใช่ :00) ใช้ชื่อเดิมต่อไปโดยไม่มีอะไรต้องย้าย

    คำตอบขึ้นกับ `start` อย่างเดียว ไม่ขึ้นกับ "ตอนนี้" — แถวของหน้าต่างหนึ่งจึงมีชื่อเดียว
    ตลอดอายุของมัน
    """
    kind = window if window in WINDOW_KINDS else "day"
    for longer in WINDOW_KINDS[WINDOW_KINDS.index(kind) + 1:]:
        if window_bounds(longer, start)[0] == start:
            return f"{subject}@{kind}"
    return subject


class CounterStore(ABC):
    """ตัวนับของ (subject, ชนิดหน้าต่าง) ในหน้าต่างปัจจุบัน

    `key` ที่รับเข้ามาคือ *subject* (`user:<id>`, `key:<id>`, …) ไม่ใช่ชื่อแถว —
    ชื่อแถวเป็นเรื่องของ store และต้องผ่าน `counter_key` เสมอ
    """

    @abstractmethod
    async def get(self, key: str, window: str) -> Consumption: ...

    @abstractmethod
    async def increment(self, key: str, window: str, delta: Consumption) -> None: ...

    @abstractmethod
    async def reset(self, key: str, window: str) -> None:
        """Put this subject's current window back to zero.

        Only the counter. Usage records are a separate ledger and are what the
        reports are built from — clearing a quota must not erase the evidence
        of what was spent.
        """

    async def reserve(self, key: str, window: str, limit: int) -> bool:
        """นับคำขอหนึ่งคำขอเข้าหน้าต่างนี้ **ถ้ายังมีที่** · False = เต็มแล้ว และไม่ได้นับ

        `limit` 0 = ไม่จำกัด (นับเสมอ) · คำขอที่ถูกปฏิเสธต้องไม่กินที่: ไม่งั้น client ที่
        ยิงซ้ำระหว่างรอจะดันตัวนับขึ้นไปเรื่อย ๆ ทั้งที่ไม่มีคำขอไหนได้ทำงาน

        ตัวตั้งต้นนี้อ่านแล้วค่อยบวก — ไม่ atomic · มีไว้ให้ store ที่ไม่ได้เขียนของตัวเอง
        ยังทำงานได้ · store ที่ใช้จริงทั้งสองตัวเขียนทับด้วยคำสั่งเดียวที่ที่เก็บเป็นคนตัดสิน
        """
        if limit and (await self.get(key, window)).requests >= limit:
            return False
        await self.increment(key, window, Consumption(requests=1))
        return True

    async def release(self, key: str, window: str) -> None:
        """คืนที่ที่ `reserve` จองไว้ — คำขอนั้นถูกปฏิเสธที่ด่านถัดไป ไม่ได้ถูกส่งต่อ"""
        await self.increment(key, window, Consumption(requests=-1))


# รอบลองใหม่เมื่อ SQLite บอกว่าไฟล์ถูกล็อก — สั้น ๆ พอให้คนเขียนที่คิวหน้าเขียนจบ
# ไม่ยาวจนคำขอของผู้ใช้ค้าง · Postgres ไม่เคยเข้าเส้นนี้ (ล็อกระดับแถว ไม่ใช่ทั้งไฟล์)
_LOCK_RETRIES = 4
_LOCK_BACKOFF_S = 0.05


class DatabaseCounterStore(CounterStore):
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def get(self, key: str, window: str) -> Consumption:
        start, end = window_bounds(window)
        key = counter_key(key, window, start)
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
        """บวกเข้าไปในตัวนับของ (subject, window) นี้ — ให้ฐานข้อมูลเป็นคนบวก

        เดิมเป็น read-modify-write ในภาษา Python: SELECT แถวออกมา, `+=` ในหน่วยความจำ,
        แล้ว UPDATE ด้วยยอดรวมใหม่ · สอง worker ที่อ่านแถวเดียวกันในจังหวะเดียวกันจึงต่าง
        เขียนยอดของตัวเองทับกัน และ **หนึ่งในสองการบวกหายไป** — เสมอในทางที่เป็นคุณกับ
        ผู้ใช้ และยิ่งคนใช้พร้อมกันเยอะก็ยิ่งหายเยอะ คือหายมากที่สุดตอนที่โควตาเป็นสิ่งที่
        เรากำลังพึ่งพาอยู่พอดี

        บน SQLite อาการเบากว่าเพราะ SQLite ให้เขียนได้ทีละคนอยู่แล้ว (และ WAL +
        busy_timeout ทำให้คนที่มาทีหลังรอแทนที่จะแพ้) — แต่ช่วงอ่านของอีกฝั่งเกิดไป
        ก่อนหน้านั้นแล้ว การบวกก็ยังหายอยู่ดี · **บน Postgres ที่รับการเขียนพร้อมกัน
        ได้จริง อาการนี้หนักขึ้นตามจำนวน instance** ซึ่งคือเหตุผลเดียวที่ลูกค้าย้ายมา

        `UPDATE ... SET requests = requests + :n` ให้ฐานข้อมูลอ่านและบวกใต้ row lock
        เดียวกัน การบวกจึงหายไม่ได้บนทั้งสอง dialect · เขียนด้วย Core update() ล้วน
        ไม่มีไวยากรณ์เฉพาะ dialect (ไม่ใช้ INSERT .. ON CONFLICT ซึ่ง SQLite เก่า
        ไม่รองรับ และสะกดคนละแบบกับ Postgres)

        Redis ยังเป็นทางที่แนะนำสำหรับ deployment หลาย worker อยู่เหมือนเดิม — อันนี้
        แก้ให้ทางสำรองถูกต้อง ไม่ได้มาแทนที่
        """
        start, end = window_bounds(window)
        key = counter_key(key, window, start)

        # SQLite ให้เขียนได้ทีละคน · WAL + busy_timeout ทำให้คนที่มาทีหลัง *รอ* แทนที่จะแพ้
        # แต่รอจนหมดเวลาก็ยังเป็นไปได้เมื่อคนเขียนเยอะพร้อมกันบนดิสก์ช้า (เจอบน CI runner:
        # 60 การบวกพร้อมกัน → "database is locked") · ยอมแพ้ตรงนี้ = การบวกหายจริง
        # ซึ่งคือสิ่งเดียวกับที่เมธอดนี้ถูกเขียนขึ้นมาแก้ · ลองใหม่แบบถอยเพิ่มทีละรอบ
        for attempt in range(_LOCK_RETRIES):
            try:
                async with self._session_factory() as session:
                    if await self._add_to_existing(session, key, start, delta):
                        return
                break
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == _LOCK_RETRIES - 1:
                    raise
                await asyncio.sleep(_LOCK_BACKOFF_S * (attempt + 1))

        # ยังไม่มีแถวของหน้าต่างนี้ — สร้างพร้อมยอดของรอบนี้ไปเลย
        async with self._session_factory() as session:
            session.add(
                QuotaCounter(
                    subject_key=key,
                    window_start=start,
                    window_end=end,
                    requests=delta.requests,
                    text_input_tokens=delta.text_input_tokens,
                    visual_input_tokens=delta.visual_input_tokens,
                    output_tokens=delta.output_tokens,
                    images=delta.images,
                )
            )
            try:
                await session.commit()
                return
            except IntegrityError:
                # อีก worker แทรกแถวเดียวกันเข้ามาระหว่าง UPDATE กับ INSERT ของเรา
                # (uq_counter_window เป็นตัวจับ) — ไม่ใช่ข้อผิดพลาด แค่แพ้การแข่ง
                await session.rollback()

        async with self._session_factory() as session:
            if not await self._add_to_existing(session, key, start, delta):
                # ถึงตรงนี้แปลว่าแถวหายไปอีกรอบหลังเพิ่งถูกสร้าง — เป็นไปได้ทางเดียวคือ
                # มีคน reset โควตาพอดี · บันทึกไว้ ไม่โยนต่อ: การนับพลาดหนึ่งครั้งไม่ควร
                # ทำให้คำขอที่ตอบไปเรียบร้อยแล้วกลายเป็น error
                log.error("โควตา: บวกตัวนับของ %s ไม่สำเร็จหลังลองใหม่", key)

    @staticmethod
    async def _add_to_existing(
        session: AsyncSession, key: str, start: datetime, delta: Consumption
    ) -> bool:
        """บวกลงแถวที่มีอยู่ · False = ยังไม่มีแถวนั้น (ไม่ได้เขียนอะไรเลย)

        coalesce ไว้เพราะแถวที่สร้างโดยเวอร์ชันก่อน 1.6 อาจมีตัวนับเป็น NULL และ
        `NULL + 5` ใน SQL คือ NULL — ตัวนับจะถูกล้างเงียบ ๆ แทนที่จะเพิ่ม
        """
        result = await session.execute(
            update(QuotaCounter)
            .where(
                QuotaCounter.subject_key == key,
                QuotaCounter.window_start == start,
            )
            .values(
                requests=func.coalesce(QuotaCounter.requests, 0) + delta.requests,
                text_input_tokens=(
                    func.coalesce(QuotaCounter.text_input_tokens, 0) + delta.text_input_tokens
                ),
                visual_input_tokens=(
                    func.coalesce(QuotaCounter.visual_input_tokens, 0)
                    + delta.visual_input_tokens
                ),
                output_tokens=(
                    func.coalesce(QuotaCounter.output_tokens, 0) + delta.output_tokens
                ),
                images=func.coalesce(QuotaCounter.images, 0) + delta.images,
            )
        )
        if result.rowcount:
            await session.commit()
            return True
        await session.rollback()
        return False

    async def reset(self, key: str, window: str) -> None:
        start, _ = window_bounds(window)
        key = counter_key(key, window, start)
        async with self._session_factory() as session:
            row = await self._fetch(session, key, start)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def reserve(self, key: str, window: str, limit: int) -> bool:
        """`UPDATE … SET requests = requests + 1 WHERE … AND requests < :limit`

        เงื่อนไขกับการบวกอยู่ในคำสั่งเดียว ฐานข้อมูลจึงเป็นคนตัดสินใต้ row lock:
        สิบคำขอที่มาพร้อมกันกับลิมิต 2 ได้ rowcount = 1 แค่สองตัว · บน PostgreSQL ตัวที่
        รอ lock จะประเมิน WHERE ใหม่กับค่าที่เพิ่ง commit (READ COMMITTED) และ SQLite
        ให้เขียนทีละคนอยู่แล้ว — ทางสำรองนี้จึงถูกต้องแม้มีหลาย worker ไม่ใช่แค่ "พอใช้"
        """
        start, end = window_bounds(window)
        key = counter_key(key, window, start)

        if await self._take_one(key, start, limit):
            return True
        # rowcount 0 = ยังไม่มีแถวของหน้าต่างนี้ หรือเต็มแล้ว · ลองเป็นคนสร้างแถวแรก
        async with self._session_factory() as session:
            session.add(
                QuotaCounter(subject_key=key, window_start=start, window_end=end, requests=1)
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                # มีแถวอยู่แล้ว: เต็มจริง หรืออีก worker เพิ่งสร้างตัดหน้า — ถามอีกรอบ
                await session.rollback()
        return await self._take_one(key, start, limit)

    async def _take_one(self, key: str, start: datetime, limit: int) -> bool:
        for attempt in range(_LOCK_RETRIES):
            try:
                async with self._session_factory() as session:
                    result = await session.execute(self._take_one_statement(key, start, limit))
                    if result.rowcount:
                        await session.commit()
                        return True
                    await session.rollback()
                    return False
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == _LOCK_RETRIES - 1:
                    raise
                await asyncio.sleep(_LOCK_BACKOFF_S * (attempt + 1))
        return False

    @staticmethod
    def _take_one_statement(key: str, start: datetime, limit: int):
        used = func.coalesce(QuotaCounter.requests, 0)
        statement = update(QuotaCounter).where(
            QuotaCounter.subject_key == key,
            QuotaCounter.window_start == start,
        )
        if limit:
            statement = statement.where(used < limit)
        return statement.values(requests=used + 1)

    async def release(self, key: str, window: str) -> None:
        start, _ = window_bounds(window)
        key = counter_key(key, window, start)
        async with self._session_factory() as session:
            await session.execute(
                update(QuotaCounter)
                .where(
                    QuotaCounter.subject_key == key,
                    QuotaCounter.window_start == start,
                    QuotaCounter.requests > 0,
                )
                .values(requests=QuotaCounter.requests - 1)
            )
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
    def _redis_key(key: str, window: str, start: datetime) -> str:
        return f"quota:{counter_key(key, window, start)}:{start.isoformat()}"

    async def get(self, key: str, window: str) -> Consumption:
        start, _ = window_bounds(window)
        values = await self._redis.hgetall(self._redis_key(key, window, start))
        if not values:
            return Consumption()
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in values.items()
        }
        return Consumption(**{f: decoded.get(f, 0) for f in self.FIELDS})

    async def increment(self, key: str, window: str, delta: Consumption) -> None:
        start, end = window_bounds(window)
        redis_key = self._redis_key(key, window, start)
        pipe = self._redis.pipeline()
        for field_name in self.FIELDS:
            value = getattr(delta, field_name)
            if value:
                pipe.hincrby(redis_key, field_name, value)
        pipe.expire(redis_key, self._ttl(end))
        await pipe.execute()

    @staticmethod
    def _ttl(end: datetime) -> int:
        return max(int((end - datetime.now(timezone.utc)).total_seconds()), 60)

    async def reset(self, key: str, window: str) -> None:
        start, _ = window_bounds(window)
        await self._redis.delete(self._redis_key(key, window, start))

    async def reserve(self, key: str, window: str, limit: int) -> bool:
        """บวกก่อน แล้วดูว่าตัวเองเป็นลำดับที่เท่าไร

        HINCRBY คืนค่าหลังบวก และ Redis ทำทีละคำสั่ง — ทุก worker จึงได้ลำดับไม่ซ้ำกัน
        ใครได้เลขเกินลิมิตคือคนที่มาช้าไป ถอยออกด้วยการลบคืน · ระหว่างบวกกับลบคืนตัวนับ
        จะสูงเกินจริงชั่วครู่ ซึ่งไม่ทำให้ใครถูกปฏิเสธผิด: จะมีคนถอยได้ก็ต่อเมื่อที่เต็มไปแล้ว
        """
        start, end = window_bounds(window)
        redis_key = self._redis_key(key, window, start)
        pipe = self._redis.pipeline()
        pipe.hincrby(redis_key, "requests", 1)
        # ตั้งอายุไปด้วยเสมอ: คำขอที่ถูกรับแต่ไม่เคยจบ (worker ตาย) ต้องไม่ทิ้งคีย์ค้างตลอดกาล
        pipe.expire(redis_key, self._ttl(end))
        position = int((await pipe.execute())[0])
        if limit and position > limit:
            await self._redis.hincrby(redis_key, "requests", -1)
            return False
        return True

    async def release(self, key: str, window: str) -> None:
        start, _ = window_bounds(window)
        await self._redis.hincrby(self._redis_key(key, window, start), "requests", -1)


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

    async def reserve(self, key: str, window: str, limit: int) -> bool:
        """Redis ตัดสินเมื่อมันตอบ ฐานข้อมูลตัดสินเมื่อมันไม่ตอบ — ทั้งคู่ atomic

        ไม่ reseed จากฐานข้อมูลเหมือน `get`: ลิมิตที่จองตอนรับคำขอคือลิมิตต่อนาที ยอดที่
        ตกค้างอยู่อีกฝั่งหลัง Redis กลับมามีอายุไม่เกินหกสิบวินาที
        """
        if self.using_redis:
            try:
                admitted = await self._redis.reserve(key, window, limit)
                QUOTA_DEGRADED.set(0)
                return admitted
            except Exception as exc:
                self._mark_down(exc)
        return await self._database.reserve(key, window, limit)

    async def release(self, key: str, window: str) -> None:
        if self.using_redis:
            try:
                await self._redis.release(key, window)
                return
            except Exception as exc:
                self._mark_down(exc)
                # ที่ที่จองไว้อยู่ใน Redis ซึ่งเพิ่งหายไป · ไปลบที่ฐานข้อมูลแทนจะเป็นการ
                # ลบที่ของคำขออื่น — ปล่อยไว้ มันหมดอายุเองในหนึ่งนาที
                return
        await self._database.release(key, window)

    async def reset(self, key: str, window: str) -> None:
        """Both ledgers, always — clearing one is worse than clearing neither.

        Redis alone does not work: the next read misses, sees that an earlier
        outage may have left counts in the database, and reseeds from it. The
        number the admin just cleared comes straight back, and the button looks
        broken for reasons nobody can see.

        Clearing the database alone leaves Redis serving the old figure until
        the window ends. So both, and Redis is attempted even while it is marked
        down: it may have recovered, and stale counts sitting there outlive the
        outage that caused the fallback.
        """
        await self._database.reset(key, window)
        try:
            await self._redis.reset(key, window)
        except Exception as exc:
            self._mark_down(exc)
            # ล้างไม่ครบแล้วบอกว่าสำเร็จ = ผู้ดูแลเห็นตัวเลขเดิมโผล่กลับมาโดยไม่รู้ว่าทำไม
            raise GatewayError(
                ErrorCode.UPSTREAM_ERROR,
                "Cleared the stored counters, but Redis is unreachable and may "
                "still hold this window's count. Try again once it is back.",
            ) from exc


class QuotaService:
    def __init__(self, store: CounterStore, defaults: QuotaDefaults) -> None:
        self._store = store
        self._defaults = defaults

    def update_defaults(self, defaults: QuotaDefaults) -> None:
        self._defaults = defaults

    @staticmethod
    def subject_key(
        user_id: str,
        model_alias: str | None = None,
        *,
        workspace_id: str | None = None,
        access_group_id: str | None = None,
    ) -> str:
        """ชื่อกองที่นับการใช้งานของคนคนนี้ **ภายใต้เป้าหมายหนึ่ง**

        กองเป็นของ (คน, เป้าหมาย) ไม่ใช่ของคนอย่างเดียว · เดิมทุกนโยบายอ่านกอง
        `user:<id>` กองเดียว นโยบาย "coding ได้ 2 ครั้งต่อวัน" จึงถูกวัดกับทุกโมเดลที่คน
        คนนั้นเรียก — เรียก gemma-vision สองครั้ง แล้ว coding ครั้งแรกในชีวิตได้ 429
        "(2 of 2)" (ตรวจพบ 2026-10-06) ทั้งที่ README บอกว่าโควตาตั้งได้ "ต่อสมาชิก
        workspace โมเดล หรือกลุ่ม"

          user:<u>                          นโยบายที่ไม่เล็งอะไร (global · user · default)
          user:<u>:ws:<w>                   นโยบายของ workspace
          user:<u>:model:<alias>            นโยบายที่เล็งโมเดลเดียว
          user:<u>:group:<g>                นโยบายที่เล็งมัดโมเดล
          user:<u>:ws:<w>:model:<alias>     ทั้งสองอย่าง (และ :group: เช่นกัน)

        **กองที่ไม่เล็งอะไรใช้ชื่อเดิม** — ตัวนับของทุกคนที่ production นับอยู่ไม่ขยับ
        ส่วนกองของนโยบายที่มีเป้าหมายเป็นชื่อใหม่ เริ่มนับจากศูนย์ครั้งเดียวตอน deploy
        (ยอดเดิมของมันคือยอดรวมทุกโมเดล ซึ่งไม่ใช่ตัวเลขที่นโยบายนั้นควรถูกวัดด้วยอยู่แล้ว)
        """
        key = f"user:{user_id}"
        if workspace_id:
            key += f":ws:{workspace_id}"
        if model_alias:
            key += f":model:{model_alias}"
        elif access_group_id:
            key += f":group:{access_group_id}"
        return key

    @staticmethod
    def key_subject(api_key_id: str) -> str:
        """ตัวนับของ key ใบเดียว — แยกกองจากของคน

        คนละกองกันโดยตั้งใจ: ใบหนึ่งหมดโควตาไม่ควรลากใบอื่นของคนเดียวกันไปด้วย และ
        เจ้าของยังต้องไม่เกินโควตารวมของตัวเองอยู่ดี
        """
        return f"key:{api_key_id}"

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

        คำขอหนึ่งคำขออยู่ใต้นโยบายเดียว และ **ถูกนับลงกองของนโยบายนั้น** (`subject`)
        — กองของเป้าหมายที่มันเล็ง ไม่ใช่กองรวมของคน
        """
        now = datetime.now(timezone.utc)
        result = await session.execute(
            # นโยบายของ key ไม่เกี่ยวกับการคิดโควตาของคน · ถ้าไม่กันไว้ นโยบายที่ตั้ง
            # เพดานให้ key ใบเดียว (ซึ่งไม่ได้ระบุ user/workspace) จะได้คะแนน 0
            # เท่ากับนโยบายกลาง แล้วไปชนะ — ตั้งเพดานให้ CI token ใบเดียวกลายเป็น
            # การลดโควตาของทุกคนในระบบ · เทสต์จับได้ตอนเขียนฟีเจอร์นี้พอดี
            select(QuotaPolicy).where(
                QuotaPolicy.enabled.is_(True),
                QuotaPolicy.api_key_id.is_(None),
            )
        )
        # An expired policy is skipped rather than deleted: the row is the record
        # of what was granted and when, which is the first thing anyone asks
        # afterwards.
        policies = [
            p for p in result.scalars()
            if p.expires_at is None or _aware(p.expires_at) > now
        ]

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
            if policy.api_key_id:
                return -1          # เพดานของ key คนละด่าน ไม่ใช่โควตาของคน
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
                subject=self.subject_key(user_id),
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
            policy_id=best.id,
            policy_name=best.name or "",
            subject=self.subject_key(
                user_id,
                best.model_alias,
                workspace_id=best.workspace_id,
                access_group_id=best.access_group_id,
            ),
            workspace_id=best.workspace_id,
            model_alias=best.model_alias,
            access_group_id=best.access_group_id,
        )

    async def resolve_key_limits(
        self, session: AsyncSession, api_key_id: str
    ) -> KeyLimits | None:
        """เพดานของ key ใบนี้ — ทุกอันที่มีคนตั้งไว้ · ไม่มีเลย = None

        แยกจาก resolve_limits ของคนโดยตั้งใจ — ไม่ไปแตะตรรกะที่ทางเดินของคำขอทุกคำขอ
        ใช้อยู่ · และเมื่อไม่มีนโยบายของ key (ซึ่งคือค่าเริ่มต้นของทุก deployment)
        ทางเดินจะไม่มีอะไรเปลี่ยนเลย ไม่มีการอ่านตัวนับเพิ่ม
        """
        if not api_key_id:
            return None
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(QuotaPolicy)
            .where(
                QuotaPolicy.enabled.is_(True),
                QuotaPolicy.api_key_id == api_key_id,
            )
            # ลำดับคงที่: อันที่ชนก่อนคืออันที่รายงาน และต้องเป็นอันเดิมทุกครั้ง
            .order_by(QuotaPolicy.created_at, QuotaPolicy.id)
        )
        policies = [
            p for p in result.scalars()
            if p.expires_at is None or _aware(p.expires_at) > now
        ]
        if not policies:
            return None
        # นโยบายของ key มีไว้เพื่อ *จำกัด* — ตั้งไว้กี่อันก็ต้องผ่านทุกอัน ไม่มีอันไหนแทนอันไหน
        subject = self.key_subject(api_key_id)
        return KeyLimits(
            subject=subject,
            ceilings=[
                ResolvedLimits(
                    window=p.window,
                    max_requests=p.max_requests,
                    max_input_tokens=p.max_input_tokens,
                    max_output_tokens=p.max_output_tokens,
                    max_images=p.max_images,
                    source="key",
                    max_requests_per_minute=p.max_requests_per_minute or 0,
                    max_tokens_per_minute=p.max_tokens_per_minute or 0,
                    policy_id=p.id,
                    policy_name=p.name or "",
                    subject=subject,
                )
                for p in policies
            ],
        )

    async def check_key(self, api_key_id: str, limits: KeyLimits) -> None:
        """ด่านที่สอง: ใบนี้เองยังไม่เกินเพดานของมัน — ทุกเพดาน

        เรียกหลังด่านของคนเสมอ · ข้อความที่ผู้ใช้ได้จะบอกว่าเป็นเพดานของ key ไม่ใช่
        ของตัวเขา ไม่งั้นคนที่ยังมีโควตาเหลือเยอะจะงงว่าทำไมโดนปฏิเสธ
        """
        for ceiling in limits.ceilings:
            await self._check_subject(self.key_subject(api_key_id), ceiling, subject="key")

    async def check(self, user_id: str, limits: ResolvedLimits) -> Consumption:
        return await self._check_subject(
            limits.subject or self.subject_key(user_id), limits, subject="user"
        )

    async def admit(
        self, limits: ResolvedLimits, key_limits: KeyLimits | None = None
    ) -> Charge:
        """รับคำขอนี้เข้า และบอกว่ามันจะถูกนับลงกองไหนเมื่อจบ

        เรียกหลังด่าน `check`/`check_key` ผ่านแล้ว ก่อนส่งต่อให้ backend · ใบที่ได้ต้อง
        ถูกส่งต่อให้ `record()` — ถ้าไม่ส่ง record จะนับลงกองรวมของคน (พฤติกรรมเดิม)
        ซึ่งผิดกองสำหรับนโยบายที่มีเป้าหมาย

        **ลิมิตคำขอต่อนาทีถูกนับตรงนี้ ไม่ใช่ตอนคำขอจบ** · เดิม `check` อ่านตัวนับ
        และ `record` บวกหลังคำตอบจบ — คำขอสิบตัวที่มาพร้อมกันจึงอ่านเจอ 0 ทั้งสิบตัว
        (ตรวจพบ 2026-10-06: ลิมิต 2 ครั้ง/นาที backend ตอบช้า 0.4 วินาที → ผ่านสิบตัว
        แล้วตัวที่ 11 ได้ "(10 of 2)") และ stream ยาว ๆ ถูกนับในนาทีที่มันจบ ไม่ใช่นาทีที่
        มันเริ่มกินเครื่อง · ตัวกัน burst ที่นับตอนจบกันได้ทุกอย่างยกเว้น burst

        จองทีละด่าน (ของคน แล้วของ key) · ด่านหลังเต็ม = คืนที่ที่ด่านแรกจองไว้ ไม่งั้น
        key ที่ชนเพดานของตัวเองจะกินลิมิตต่อนาทีของเจ้าของไปทุกครั้งที่ลองใหม่
        """
        gates: list[tuple[str, int, str]] = []
        if limits.rate_limited:
            gates.append((limits.subject, limits.max_requests_per_minute, "user"))
        if key_limits is not None and key_limits.rate_limited:
            gates.append((key_limits.subject, key_limits.max_requests_per_minute, "key"))

        held: list[str] = []
        for subject, per_minute, whose in gates:
            if await self._store.reserve(subject, "minute", per_minute):
                held.append(subject)
                continue
            for earlier in held:
                try:
                    await self._store.release(earlier, "minute")
                except Exception:
                    log.exception("โควตา: คืนที่ของ %s ในนาทีนี้ไม่สำเร็จ", earlier)
            # ที่เต็มพอดี — ไม่อ่านตัวนับซ้ำเพื่อเอาเลขมาโชว์ เพราะเลขนั้นรวมคำขอที่
            # กำลังถอยออกอยู่ และ "10 of 2" ไม่ได้บอกอะไรที่ "2 of 2" ไม่ได้บอก
            raise _exhausted("request", per_minute, per_minute, "minute", whose)

        windows: list[tuple[str, str]] = [(limits.subject, limits.window)]
        minutes: list[str] = [limits.subject] if limits.rate_limited else []
        if key_limits is not None:
            # เพดานแต่ละอันมีหน้าต่างของตัวเอง — นับลงทุกหน้าต่างที่มีเพดานอ่านอยู่
            windows.extend((key_limits.subject, window) for window in key_limits.windows)
            if key_limits.rate_limited:
                minutes.append(key_limits.subject)
        return Charge(windows=tuple(dict.fromkeys(windows)), minutes=tuple(minutes))

    async def _check_subject(
        self, key: str, limits: ResolvedLimits, *, subject: str = "user"
    ) -> Consumption:
        used = await self._store.get(key, limits.window)

        def exceeded(name: str, used_value: int, limit: int, window: str) -> None:
            if not limit or used_value < limit:
                return
            raise _exhausted(name, used_value, limit, window, subject)

        # The burst check comes first. Both can be over at once, and being told
        # to wait forty seconds is a more useful answer than being told to come
        # back tomorrow when the daily figure was not the binding one.
        #
        # อ่านอย่างเดียว: ด่านนี้ตอบคนที่เกินไปแล้วโดยไม่เขียนอะไร · การนับคำขอเข้าตัวนับ
        # นาทีจริง ๆ ทำที่ admit() หลังทุกด่านผ่าน — ถ้านับตรงนี้ คำขอที่ไปตกด่านโควตา
        # รายวันในบรรทัดถัดไปจะกินที่ของนาทีนี้ไปฟรี ๆ
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
        self,
        user_id: str,
        window: str,
        delta: Consumption,
        *,
        rate_limited: bool = False,
        api_key_id: str = "",
        key_window: str = "",
        key_rate_limited: bool = False,
        charge: Charge | None = None,
    ) -> None:
        if charge is not None:
            # กองถูกตัดสินไว้แล้วตอนรับคำขอ (admit) — ไม่เดาใหม่จาก user_id
            try:
                for subject, charged_window in charge.windows:
                    await self._store.increment(subject, charged_window, delta)
                # คำขอถูกนับเข้าตัวนับนาทีไปแล้วตอน admit — ตรงนี้เหลือแค่ token
                tokens_only = Consumption(
                    text_input_tokens=delta.text_input_tokens,
                    visual_input_tokens=delta.visual_input_tokens,
                    output_tokens=delta.output_tokens,
                    images=delta.images,
                )
                for subject in charge.minutes:
                    if tokens_only != Consumption():
                        await self._store.increment(subject, "minute", tokens_only)
            except Exception:
                log.exception("failed to record quota consumption for user %s", user_id)
            return
        try:
            key = self.subject_key(user_id)
            await self._store.increment(key, window, delta)
            # Only when a rate limit is actually set: otherwise every deployment
            # that never wanted one would pay for a second counter per request.
            if rate_limited:
                await self._store.increment(key, "minute", delta)
            # กองของ key นับเฉพาะเมื่อมีนโยบายของ key จริง ๆ · ไม่มีนโยบาย = ไม่มี
            # ตัวนับเพิ่ม ทุก deployment ที่ไม่ได้ใช้ฟีเจอร์นี้จึงไม่จ่ายอะไรเลย
            if api_key_id and key_window:
                subject = self.key_subject(api_key_id)
                await self._store.increment(subject, key_window, delta)
                if key_rate_limited:
                    await self._store.increment(subject, "minute", delta)
        except Exception:
            # Never fail a completed request because bookkeeping failed.
            log.exception("failed to record quota consumption for user %s", user_id)

    async def reset(self, user_id: str, limits: ResolvedLimits) -> None:
        """Give this person their window back.

        Unlike `record`, a failure here is not swallowed. Bookkeeping that fails
        quietly costs a few tokens of accuracy; a reset that fails quietly
        leaves somebody locked out while the console says it worked.
        """
        key = limits.subject or self.subject_key(user_id)
        await self._store.reset(key, limits.window)
        if limits.rate_limited:
            await self._store.reset(key, "minute")

    async def usage_snapshot(self, user_id: str, limits: ResolvedLimits) -> dict:
        used = await self._store.get(
            limits.subject or self.subject_key(user_id), limits.window
        )
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


def _exhausted(name: str, used_value: int, limit: int, window: str, subject: str) -> GatewayError:
    """429 ของโควตา — ข้อความเดียวกันไม่ว่าจะถูกจับที่ด่านอ่าน (check) หรือตอนจอง (admit)"""
    wait = _seconds_to_reset(window)
    when = (
        f"It clears in {wait} second{'s' if wait != 1 else ''}."
        if window == "minute"
        else f"It resets at the start of the next {window}."
    )
    whose = "This API key's" if subject == "key" else "Your"
    return GatewayError(
        ErrorCode.QUOTA_EXCEEDED,
        f"{whose} {window} {name} quota is exhausted "
        f"({used_value:,} of {limit:,}). {when}",
        retry_after=wait,
        details={
            "quota": name,
            "subject": subject,
            "used": used_value,
            "limit": limit,
            "window": window,
            "resets_at": window_bounds(window)[1].isoformat(),
        },
    )


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; normalise before comparing."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _seconds_to_reset(window: str) -> int:
    _, end = window_bounds(window)
    return max(int((end - datetime.now(timezone.utc)).total_seconds()), 1)
