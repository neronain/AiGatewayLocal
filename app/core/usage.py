"""Usage recording (PRD §10, §11).

Rows are metadata only. There is no column for prompt text, response text, or
image bytes, so the privacy default (`store_prompts=false`) cannot be violated
by a code change without a schema change and a review.

Writes are buffered and flushed by a background task: a slow database must never
add latency to an inference response, and losing usage rows is preferable to
failing member requests.

What a failed flush costs is bounded per row, not per batch (2026-10-05):

  * one row the database refuses (a duplicate id, a value that does not fit a
    column) is dropped *alone*, named in the log and counted in `rejected` - it
    used to take every other member's row in the same batch with it;
  * a database that is locked or unreachable is nobody's row's fault, so the
    whole batch goes back in the buffer for the next flush, up to MAX_BUFFER.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.auth import Principal
from app.core.multimodal import RequestProfile
from app.core.tokens import TokenUsage
from app.db.models import UsageLog, utcnow
from app.db.session import session_scope

log = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 2.0
MAX_BUFFER = 5000
# เท่ากับความกว้างของคอลัมน์ `usage_logs.client_request_id` · ตัดที่นี่เพราะ Postgres
# ปฏิเสธค่าที่ยาวเกินคอลัมน์ (SQLite ไม่) และค่านี้มาจาก header ที่ client เขียนอะไรก็ได้
CLIENT_REQUEST_ID_MAX = 128

# ความล้มเหลวที่เป็นเรื่องของ *ฐานข้อมูล* ไม่ใช่ของแถวใดแถวหนึ่ง: ล็อกอยู่ ต่อไม่ติด pool เต็ม
# ลองทีละแถวกับของพวกนี้คือรอ timeout เดิมซ้ำอีก N รอบ (SQLite รอ busy_timeout 15 วินาที
# ต่อครั้ง) · อย่างอื่นทั้งหมดถือว่าเป็นความผิดของแถว — ถ้าเดาผิดทางนั้น เสียแถวเดียว
# ถ้าเดาผิดทางนี้ แถวพิษหนึ่งแถวจะถูกลองใหม่ไม่รู้จบและกั้นทุกแถวที่ตามหลัง
_DATABASE_UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeout, OSError)


def clean_client_request_id(value: str | None) -> str | None:
    """ค่า `x-request-id` ของ client ในรูปที่เก็บได้ · None = ไม่ได้ส่งมา"""
    value = (value or "").strip()
    return value[:CLIENT_REQUEST_ID_MAX] or None


@dataclass
class UsageRecord:
    # id ที่เกตเวย์สร้างเอง — ห้ามเป็นค่าที่รับมาจาก client (ดู UsageLog.request_id)
    request_id: str
    model_alias: str
    protocol: str
    client_request_id: str | None = None
    ts: datetime = field(default_factory=utcnow)
    user_id: str | None = None
    workspace_id: str | None = None
    api_key_id: str | None = None
    endpoint_name: str = ""
    request_modality: str = "text"
    stream: bool = False
    text_input_tokens: int = 0
    visual_input_tokens: int = 0
    output_tokens: int = 0
    image_count: int = 0
    token_accounting: str = "estimated"
    latency_ms: int = 0
    ttft_ms: int | None = None
    status: str = "success"
    http_status: int = 200
    error_code: str | None = None
    client_agent: str = ""

    @property
    def total_tokens(self) -> int:
        return self.text_input_tokens + self.visual_input_tokens + self.output_tokens

    def to_row(self) -> UsageLog:
        return UsageLog(
            request_id=self.request_id,
            client_request_id=clean_client_request_id(self.client_request_id),
            ts=self.ts,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            api_key_id=self.api_key_id,
            model_alias=self.model_alias,
            endpoint_name=self.endpoint_name,
            protocol=self.protocol,
            request_modality=self.request_modality,
            stream=self.stream,
            text_input_tokens=self.text_input_tokens,
            visual_input_tokens=self.visual_input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            image_count=self.image_count,
            token_accounting=self.token_accounting,
            latency_ms=self.latency_ms,
            ttft_ms=self.ttft_ms,
            status=self.status,
            http_status=self.http_status,
            error_code=self.error_code,
            client_agent=self.client_agent[:128],
        )


def build_record(
    *,
    request_id: str,
    principal: Principal | None,
    model_alias: str,
    protocol: str,
    profile: RequestProfile | None,
    usage: TokenUsage | None,
    endpoint_name: str = "",
    stream: bool = False,
    latency_ms: int = 0,
    ttft_ms: int | None = None,
    status: str = "success",
    http_status: int = 200,
    error_code: str | None = None,
    client_agent: str = "",
    client_request_id: str | None = None,
) -> UsageRecord:
    return UsageRecord(
        request_id=request_id,
        client_request_id=client_request_id,
        model_alias=model_alias,
        protocol=protocol,
        user_id=principal.user_id if principal else None,
        workspace_id=principal.workspace_id if principal else None,
        api_key_id=principal.api_key_id if principal else None,
        endpoint_name=endpoint_name,
        request_modality=profile.request_modality if profile else "text",
        image_count=profile.image_count if profile else 0,
        stream=stream,
        text_input_tokens=usage.text_input_tokens if usage else 0,
        visual_input_tokens=usage.visual_input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        token_accounting=usage.accounting if usage else "estimated",
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        status=status,
        http_status=http_status,
        error_code=error_code,
        client_agent=client_agent,
    )


class UsageRecorder:
    def __init__(self) -> None:
        self._buffer: list[UsageRecord] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._dropped = 0
        # แถวที่ฐานข้อมูลปฏิเสธและถูกทิ้ง (นับสะสมตลอดอายุ process)
        self.rejected = 0
        # flush ที่ล้มติดกันเพราะฐานข้อมูลใช้ไม่ได้ · 0 = ปกติ
        self._outage = 0

    @property
    def pending(self) -> int:
        """จำนวนแถวที่ยังไม่ถึงฐานข้อมูล"""
        return len(self._buffer)

    async def submit(self, record: UsageRecord) -> None:
        async with self._lock:
            if len(self._buffer) >= MAX_BUFFER:
                self._count_dropped(1)
                return
            self._buffer.append(record)

    def _count_dropped(self, count: int) -> None:
        before, self._dropped = self._dropped, self._dropped + count
        # บรรทัดแรก แล้วทุกครั้งที่ข้ามร้อย — ไม่ให้ log ท่วมตอนฐานข้อมูลล่มนาน
        if before // 100 != self._dropped // 100 or before == 0:
            log.error("usage buffer full; dropped %d record(s)", self._dropped)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._flush_loop(), name="usage-flush")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()  # drain on shutdown
        if self._buffer:
            log.error(
                "shutting down with %d usage record(s) the database never accepted",
                len(self._buffer),
            )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            try:
                await self.flush()
            except Exception:
                log.exception("usage flush failed")

    async def flush(self) -> int:
        """Persist what is buffered. Returns how many rows reached the database."""
        async with self._lock:
            pending, self._buffer = self._buffer, []
        if not pending:
            return 0
        try:
            async with session_scope() as session:
                session.add_all([record.to_row() for record in pending])
            self._recovered(len(pending))
            return len(pending)
        except _DATABASE_UNAVAILABLE:
            await self._put_back(pending)
            return 0
        except Exception as exc:
            # ไม่ใช่ log.exception: แถวที่ผิดจะถูกชี้ตัวข้างล่างพร้อมเหตุผลของมันเอง
            log.warning(
                "usage batch of %d was refused (%s); writing the rows one at a time",
                len(pending), type(exc).__name__,
            )
        return await self._flush_one_at_a_time(pending)

    async def _flush_one_at_a_time(self, pending: list[UsageRecord]) -> int:
        """ทางสำรองเมื่อ batch ถูกปฏิเสธ — แถวละ transaction หาแถวที่ผิดแล้วทิ้งเฉพาะแถวนั้น"""
        saved = 0
        for position, record in enumerate(pending):
            try:
                async with session_scope() as session:
                    session.add(record.to_row())
                saved += 1
            except _DATABASE_UNAVAILABLE:
                await self._put_back(pending[position:])
                break
            except Exception as exc:
                self.rejected += 1
                log.error(
                    "usage record dropped: the database refused request %s "
                    "(client id %r, model %s, user %s, %d tokens): %s",
                    record.request_id, record.client_request_id, record.model_alias,
                    record.user_id, record.total_tokens, getattr(exc, "orig", exc),
                )
        return saved

    async def _put_back(self, records: list[UsageRecord]) -> None:
        """คืนแถวที่ยังไม่ได้เขียนไว้หัวคิว ให้ flush รอบหน้าลองใหม่ · ไม่เกิน MAX_BUFFER

        ปลอดภัยที่จะลองซ้ำ: `request_id` เป็น UNIQUE และเกตเวย์สร้างเอง แถวที่จริง ๆ แล้ว
        เขียนสำเร็จไปก่อนหน้า (commit ผ่านแต่คำตอบหายกลางทาง) จะถูกปฏิเสธเป็นแถวซ้ำ ไม่ถูกนับสองครั้ง
        """
        async with self._lock:
            room = max(0, MAX_BUFFER - len(self._buffer))
            keep, overflow = records[:room], len(records) - room
            self._buffer[:0] = keep
            waiting = len(self._buffer)
            if overflow > 0:
                self._count_dropped(overflow)
        # รอบแรกของเหตุการณ์ให้ traceback เต็ม · รอบถัดไปบรรทัดเดียว — batch เดิมถูกลองใหม่ทุก
        # 2 วินาทีตลอดช่วงที่ฐานข้อมูลใช้ไม่ได้ traceback ซ้ำทุกรอบคือ log ที่ไม่มีใครอ่านออก
        self._outage += 1
        message = "database unavailable; %d usage record(s) kept for the next flush"
        if self._outage == 1:
            log.exception(message, waiting)
        else:
            log.warning(message + " (attempt %d)", waiting, self._outage)

    def _recovered(self, written: int) -> None:
        if self._outage:
            log.warning(
                "database is back after %d failed flush(es); %d usage record(s) written",
                self._outage, written,
            )
            self._outage = 0
