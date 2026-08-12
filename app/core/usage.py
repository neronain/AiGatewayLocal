"""Usage recording (PRD §10, §11).

Rows are metadata only. There is no column for prompt text, response text, or
image bytes, so the privacy default (`store_prompts=false`) cannot be violated
by a code change without a schema change and a review.

Writes are buffered and flushed by a background task: a slow database must never
add latency to an inference response, and losing at most one flush window of
usage rows is preferable to failing member requests.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.core.auth import Principal
from app.core.multimodal import RequestProfile
from app.core.tokens import TokenUsage
from app.db.models import UsageLog, utcnow
from app.db.session import session_scope

log = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 2.0
MAX_BUFFER = 5000


@dataclass
class UsageRecord:
    request_id: str
    model_alias: str
    protocol: str
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
) -> UsageRecord:
    return UsageRecord(
        request_id=request_id,
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

    async def submit(self, record: UsageRecord) -> None:
        async with self._lock:
            if len(self._buffer) >= MAX_BUFFER:
                self._dropped += 1
                if self._dropped % 100 == 1:
                    log.error("usage buffer full; dropped %d record(s)", self._dropped)
                return
            self._buffer.append(record)

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

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            try:
                await self.flush()
            except Exception:
                log.exception("usage flush failed")

    async def flush(self) -> int:
        async with self._lock:
            pending, self._buffer = self._buffer, []
        if not pending:
            return 0
        try:
            async with session_scope() as session:
                session.add_all([record.to_row() for record in pending])
            return len(pending)
        except Exception:
            log.exception("could not persist %d usage record(s)", len(pending))
            return 0
