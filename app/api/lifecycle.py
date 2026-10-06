"""วงจรชีวิตของคำขอหนึ่งคำขอที่ไปถึง backend — ใช้ร่วมกันทั้งสาม surface

    จองช่อง -> เรียก -> (ล้ม: สลับเครื่อง -> สลับโมเดลสำรอง) -> ส่งต่อ -> คืนช่อง -> บันทึก

`/v1/chat/completions` · `/v1/messages` · `/v1/responses` เคยมีวงจรนี้คนละสำเนา (และแบบ
stream กับไม่ stream อีกอย่างละสำเนา) · บั๊กที่ตรวจพบ 2026-10-06 เกือบทั้งหมดคือสำเนาที่
ไม่ตรงกัน หรือรูที่มีครบทุกสำเนา:

* `router.acquire()` คือด่านช่องตัวจริง แต่ถูกเรียก *ใน* generator หลังส่ง 200 ไปแล้ว และอยู่
  นอก `except GatewayError` — ช่องเต็มที่ worker อื่นถืออยู่ = สายขาดกลางอากาศ บันทึกว่า
  success และไม่เคยลองโมเดลสำรอง
* สายไป backend ขาดหลัง 200 = generator `return` เงียบ ๆ ผู้เรียกได้คำตอบครึ่งเดียวที่จบ
  เหมือนจบปกติ

หลักที่ไฟล์นี้ยึด: **สถานะ HTTP กับ header ของ stream ตัดสินจากสิ่งที่เกิดก่อน event แรก**
เปิดสายไป backend และรอ payload แรกให้ได้ก่อน แล้วค่อยเริ่มตอบผู้เรียก · ก่อนจุดนั้นทุกความ
ล้มเหลวสลับเครื่องได้และตอบเป็นสถานะจริง (429/502/503/504) · หลังจุดนั้นทุกอย่างอยู่ในสาย
และผู้เรียกต้องได้ event ปิดท้ายของ surface นั้นเสมอ
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.core import jsonio
from app.core.errors import ErrorCode, GatewayError, describe
from app.core.routing import RETRYABLE_ERRORS, is_retryable_status
from app.core.tokens import TokenUsage, resolve_usage
from app.registry.schema import Endpoint
from app.upstream import client as upstream
from app.upstream.sse import DONE, iter_sse_payloads, parse_chunk

log = logging.getLogger(__name__)

# ภาษาที่ backend พูดในรอบนั้น — ไม่ใช่ surface ที่คำขอเข้ามา: /v1/messages ที่ไปถึงเครื่อง
# ที่พูดแต่ chat completions คือ OPENAI
OPENAI, ANTHROPIC, RESPONSES = "openai", "anthropic", "responses"

# ผู้เรียกตัดสายไปเอง · 499 คือเลขที่ nginx ใช้กับกรณีเดียวกัน ไม่มีใน RFC แต่คนดู log รู้จัก
CLIENT_CLOSED_REQUEST = "CLIENT_CLOSED_REQUEST"
CLIENT_CLOSED_STATUS = 499


def rate_of(ctx) -> float | None:  # noqa: ANN001
    """อัตราอักขระนอก ASCII ต่อ token ของโมเดลที่ *เสิร์ฟจริง* ตอนนี้ (fallback เปลี่ยนได้)"""
    spec = getattr(getattr(ctx, "model", None), "spec", None)
    return getattr(spec, "wide_chars_per_token", None)


@dataclass
class Call:
    """คำขอหนึ่งรอบถึงเครื่องหนึ่งเครื่อง — สร้างใหม่ทุกครั้งที่สลับเครื่อง"""

    path: str
    payload: dict[str, Any]
    headers: dict[str, str]
    dialect: str = OPENAI
    # ของ surface เอง (เช่น _Attempt ของ /v1/messages) — ไฟล์นี้ไม่อ่าน
    extra: Any = None
    # ผู้เรียกขอ usage chunk มาเองไหม · เราขอจาก backend เสมอ แต่ส่งต่อเฉพาะเมื่อเขาขอ
    client_wants_usage: bool = False


Plan = Callable[[Endpoint], Call]


def ask_for_usage(call: Call) -> None:
    """Ask for a final usage chunk so accounting stays authoritative.

    If the caller did not want it, it is stripped before forwarding so the shape
    matches what they asked for.
    """
    if call.dialect != OPENAI:
        return
    options = call.payload.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    call.client_wants_usage = bool(options.get("include_usage"))
    options["include_usage"] = True
    call.payload["stream_options"] = options


# ---------------------------------------------------------------------------
# ความล้มเหลว: นับสุขภาพเครื่อง · ตัดสินว่าสลับได้ไหม · บันทึกแถว usage
# ---------------------------------------------------------------------------
def _report(ctx, served: str, endpoint: Endpoint, exc: GatewayError) -> None:  # noqa: ANN001
    ctx.state.router.report_failure(served, endpoint, exc.message)


def _can_retry(exc: GatewayError) -> bool:
    return exc.code in RETRYABLE_ERRORS


async def _record_failure(ctx, exc: GatewayError) -> None:  # noqa: ANN001
    await ctx.finalize(
        resolve_usage(ctx.profile, None, rate_of(ctx)),
        status="error",
        http_status=exc.http_status,
        error_code=exc.code,
    )


async def take_slot(ctx, served: str, endpoint: Endpoint) -> bool:  # noqa: ANN001
    """จองช่องบนเครื่องนี้ · False = เต็ม และ ctx ถูกย้ายไปเครื่องถัดไปแล้ว (ให้ผู้เรียกวนใหม่)

    `acquire()` คือด่านช่องตัวจริง — `select()` ดูแค่คำใบ้ของ process นี้ ซึ่งมองไม่เห็นช่องที่
    worker อื่นถืออยู่ (production: 4 worker + Redis) · ถูกปฏิเสธตรงนี้จึงต้องได้สิ่งเดียวกับ
    ถูกปฏิเสธที่ select: ลองเครื่องถัดไป แล้วโมเดลสำรอง แล้วค่อยตอบ 429

    เมื่อไม่เหลือทางไป: บันทึกแถว error (เดิมทางที่ไม่ stream ไม่มีแถวเลย ส่วน stream บันทึก
    ว่า success) และ **ไม่หักโควตา** — ไม่มี backend ไหนได้เห็นคำขอนี้
    """
    try:
        await ctx.state.router.acquire(served, endpoint, ctx.lease)
        return True
    except GatewayError as exc:
        nxt = ctx.another_endpoint()
        if nxt is None:
            await ctx.finalize(
                TokenUsage(),
                status="error",
                http_status=exc.http_status,
                error_code=exc.code,
                charge=False,
            )
            raise
        ctx.retarget(nxt)
        return False


# ---------------------------------------------------------------------------
# ไม่ stream
# ---------------------------------------------------------------------------
async def complete(ctx, plan: Plan) -> tuple[Call, Endpoint, dict[str, Any]]:  # noqa: ANN001
    """ถามเครื่องหนึ่ง ไม่สบายก็ถามเครื่องถัดไป — คืนคำตอบ 200 ที่ parse แล้ว

    ยังไม่มีอะไรถึงผู้เรียก การลองใหม่จึงมองไม่เห็นจากฝั่งเขา · ล้มทุกทางแล้วจะบันทึกแถว
    usage และ raise GatewayError ของความล้มเหลวครั้งสุดท้าย
    """
    router = ctx.state.router
    while True:
        endpoint, served = ctx.endpoint, ctx.model.alias
        call = plan(endpoint)
        if not await take_slot(ctx, served, endpoint):
            continue
        answered = False
        try:
            response = await upstream.post_json(endpoint, call.path, call.payload, call.headers)
            if response.status_code >= 400:
                router.report_http_error(served, endpoint, response.status_code)
                retry = is_retryable_status(response.status_code)
                failure = upstream.upstream_error(
                    endpoint, response.status_code, response.text[:2000]
                )
            else:
                router.report_success(served, endpoint)
                answered = True
        except GatewayError as exc:
            _report(ctx, served, endpoint, exc)
            retry, failure = _can_retry(exc), exc
        finally:
            await router.release(served, endpoint, ctx.lease)

        if answered:
            try:
                # ไม่ใช้ response.json() เพราะมันเรียก json ของ stdlib ตายตัว · เรามีไบต์อยู่แล้ว
                return call, endpoint, jsonio.loads(response.content)
            except json.JSONDecodeError as exc:
                raise GatewayError(
                    ErrorCode.UPSTREAM_ERROR, "The model server returned a malformed response."
                ) from exc
        if retry and (nxt := ctx.another_endpoint()) is not None:
            ctx.retarget(nxt)
            continue
        await _record_failure(ctx, failure)
        raise failure


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------
Payload = tuple[str | None, str, dict[str, Any] | None]


async def _payloads(endpoint: Endpoint, response) -> AsyncIterator[Payload]:  # noqa: ANN001
    """(event, data ดิบ, chunk ที่ parse แล้วหรือ None) ของ stream จาก backend

    `[DONE]` ถูกกรองออก · สายขาดระหว่างอ่านออกมาเป็น GatewayError
    """
    async for event, data in iter_sse_payloads(upstream.iter_lines(endpoint, response)):
        if data.strip() == DONE:
            continue
        yield event, data, parse_chunk(data)


class OpenedStream:
    """stream ที่เปิดถึง backend แล้ว ถือช่องอยู่ และรู้แล้วว่าเครื่องนี้ตอบได้จริง"""

    def __init__(  # noqa: PLR0913
        self,
        ctx,  # noqa: ANN001
        call: Call,
        endpoint: Endpoint,
        served: str,
        response,  # noqa: ANN001
        rest: AsyncIterator[Payload],
        first: Payload | None,
    ) -> None:
        self._ctx = ctx
        self.call = call
        self.endpoint = endpoint
        self.served = served
        self._response = response
        self._rest = rest
        self._first = first
        self._closed = False
        self.ttft_ms: int | None = ctx.elapsed_ms if first is not None else None

    async def payloads(self) -> AsyncIterator[Payload]:
        if self._first is not None:
            first, self._first = self._first, None
            yield first
        async for item in self._rest:
            yield item

    def report(self, exc: GatewayError) -> None:
        _report(self._ctx, self.served, self.endpoint, exc)

    async def aclose(self) -> None:
        """ปิดสายและคืนช่อง — ทุกขั้นต้องรันแม้ขั้นก่อนหน้าถูกยกเลิก (ผู้เรียกตัดสาย)"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        finally:
            await self._ctx.state.router.release(self.served, self.endpoint, self._ctx.lease)


async def open_stream(ctx, plan: Plan) -> OpenedStream:  # noqa: ANN001
    """เปิด stream และรอ payload แรก — สลับเครื่อง/โมเดลจนกว่าจะได้ตัวที่ตอบ หรือหมดทาง

    เรียก **ก่อน** เริ่มตอบผู้เรียก · สำเร็จ = ctx.endpoint/ctx.model คือตัวที่จะเสิร์ฟจริง
    (header ที่ตั้งหลังจากนี้จึงไม่โกหก) · ล้ม = บันทึกแถว usage แล้ว raise GatewayError ซึ่ง
    ผู้เรียกได้เป็นสถานะ HTTP จริง ไม่ใช่ 200 ที่สายขาด

    รอถึง payload แรก ไม่ใช่แค่ header 200: backend ที่งานล้นตอบ 200 แล้วเงียบจนหมดเวลา
    (vLLM ส่ง header ก่อน token แรก) · เดิมจบที่ `HTTP 200 body=''` โดยเครื่องสำรองที่ว่างอยู่
    ไม่ถูกเรียกเลย
    """
    router = ctx.state.router
    while True:
        endpoint, served = ctx.endpoint, ctx.model.alias
        call = plan(endpoint)
        if not await take_slot(ctx, served, endpoint):
            continue

        handed_over = False
        response = None
        try:
            response = await upstream.open_stream(endpoint, call.path, call.payload, call.headers)
            if response.status_code >= 400:
                body = await upstream.read_error_body(response)
                router.report_http_error(served, endpoint, response.status_code)
                retry = is_retryable_status(response.status_code)
                failure = upstream.upstream_error(endpoint, response.status_code, body)
            else:
                rest = _payloads(endpoint, response)
                try:
                    first = await anext(rest)
                except StopAsyncIteration:
                    first = None
                router.report_success(served, endpoint)
                handed_over = True
                return OpenedStream(ctx, call, endpoint, served, response, rest, first)
        except GatewayError as exc:
            _report(ctx, served, endpoint, exc)
            retry, failure = _can_retry(exc), exc
        finally:
            if not handed_over:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await router.release(served, endpoint, ctx.lease)

        if retry and (nxt := ctx.another_endpoint()) is not None:
            ctx.retarget(nxt)
            continue
        await _record_failure(ctx, failure)
        raise failure


async def _client_gone(request: Request) -> None:
    """คืนเมื่อผู้เรียกตัดสาย — วิธีเดียวกับที่ StreamingResponse ของ Starlette ฟัง"""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def open_stream_for(request: Request, ctx, plan: Plan) -> OpenedStream | None:  # noqa: ANN001
    """`open_stream` ที่เลิกทันทีเมื่อผู้เรียกตัดสายระหว่างรอ · None = เขาไปแล้ว

    การรอ payload แรกเกิดก่อนมี response ให้ Starlette เฝ้าสาย ถ้าไม่เฝ้าเอง ผู้ใช้ที่กด Esc
    ระหว่างโมเดลอ่าน prompt (llama.cpp กับ context ยาวใช้เป็นนาที) จะถือช่องเดียวของโมเดลนั้น
    ไว้จนกว่า token แรกจะมา — คำขอถัดไปของเขาเองได้ 429
    """
    opening = asyncio.ensure_future(open_stream(ctx, plan))
    gone = asyncio.ensure_future(_client_gone(request))
    try:
        await asyncio.wait({opening, gone}, return_when=asyncio.FIRST_COMPLETED)
        # ตัวเฝ้าสายที่พังเอง (ไม่ใช่เห็น disconnect) ไม่ใช่เหตุให้ทิ้งคำขอ — รอเปิดต่อไป
        hung_up = gone.done() and not gone.cancelled() and gone.exception() is None
        if opening.done() or not hung_up:
            return await opening
    except BaseException:
        opening.cancel()
        raise
    finally:
        gone.cancel()

    opening.cancel()
    try:
        stream = await opening
    except asyncio.CancelledError:
        if not opening.cancelled():
            raise  # ตัวเราเองถูกยกเลิก ไม่ใช่ task ลูก
        stream = None
    except GatewayError:
        return None  # ล้มพอดีกับตอนเขาตัดสาย — แถว usage บันทึกไปแล้วใน open_stream
    if stream is not None:
        await stream.aclose()  # เปิดได้พอดีกับตอนเขาตัดสาย
    await ctx.finalize(
        resolve_usage(ctx.profile, None, rate_of(ctx)),
        status="aborted",
        http_status=CLIENT_CLOSED_STATUS,
        error_code=CLIENT_CLOSED_REQUEST,
    )
    return None


def stream_headers(ctx, stream: OpenedStream, protocol: str | None = None) -> dict[str, str]:  # noqa: ANN001
    """header ของ stream — สร้าง *หลัง* `open_stream` จึงบอกตัวที่เสิร์ฟจริง

    เดิม `x-litegate-served-by` ถูกตั้งก่อน generator เริ่ม ซึ่งเป็นก่อนการสลับไปโมเดลสำรอง:
    ตัวสำรองเป็นคนตอบ แต่ header บอกชื่อตัวที่ขอ (แถว usage ถูก) · ตอนนี้การตัดสินใจเรื่อง
    เครื่องจบก่อน response เริ่ม จึงไม่มีการสลับในสายหลัง header ออกไปแล้วอีก
    """
    headers = {
        "cache-control": "no-cache",
        "connection": "keep-alive",
        "x-accel-buffering": "no",  # nginx must not buffer SSE
        "x-request-id": ctx.request_id,
        "x-litegate-model": ctx.requested_alias,
        # ตัวที่ *รันจริง* — ต่างจาก x-litegate-model เมื่อกฎ routing เปลี่ยนเส้นทางหรือ
        # ล้มไปโมเดลสำรอง · สัญญากับสมาชิกยังเหมือนเดิมคือขอ alias ไหนได้ alias นั้น แต่เวลา
        # ไล่ปัญหาต้องรู้ว่าใครตอบ ไม่งั้นตัวเลขเร็ว/ช้าที่วัดได้จะถูกโยงไปผิดโมเดล
        "x-litegate-served-by": stream.served,
        "x-litegate-endpoint": stream.endpoint.name,
    }
    if protocol:
        headers["x-litegate-protocol"] = protocol
    if ctx.tried:
        headers["x-litegate-failed-over"] = ",".join(sorted(ctx.tried))
    return headers


async def relay(  # noqa: PLR0913
    ctx,  # noqa: ANN001
    stream: OpenedStream,
    produce: Callable[[], AsyncIterator[bytes]],
    render_error: Callable[[GatewayError], Iterable[bytes]],
    usage: Callable[[], TokenUsage],
) -> AsyncIterator[bytes]:
    """body ของ StreamingResponse — ส่วนที่เหมือนกันทุก surface

    `produce` แปล payload ของ backend เป็น frame ของ surface · ที่เหลืออยู่ที่นี่ที่เดียว:

    * **สายไป backend ขาดกลางทาง** → ผู้เรียกได้ event ปิดท้าย
      ของ surface (`render_error`) เสมอ · แถวเป็น `error` พร้อมรหัสจริง · เครื่องถูกนับว่าล้ม
    * **ผู้เรียกตัดสาย** → แถวเป็น `aborted` (เดิมค้างเป็น success/200)
    * **คืนช่อง + บันทึกแถว usage รันเสมอ** แม้ generator ถูกยกเลิก — ทั้งสองขั้น shield
      ตัวเอง และซ้อน try/finally ไว้เพราะใต้ cancel scope ของ Starlette `await` ทุกตัวใน
      `finally` โยน CancelledError ซ้ำ ขั้นที่อยู่ถัดลงไปในบล็อกเดียวกันจะไม่ได้รัน
    """
    status, error_code, http_status = "success", None, 200
    finished = False
    try:
        try:
            async for frame in produce():
                yield frame
            finished = True
        except GatewayError as exc:
            stream.report(exc)
            status, error_code, http_status = "error", exc.code, exc.http_status
            for frame in render_error(exc):
                yield frame
            finished = True
        except Exception as exc:  # noqa: BLE001 - ผู้เรียกต้องได้ event ปิดท้าย ไม่ใช่สายที่เงียบไป
            log.exception("stream failed for request %s", ctx.request_id)
            ctx.state.router.report_failure(stream.served, stream.endpoint, describe(exc))
            failure = GatewayError(
                ErrorCode.UPSTREAM_ERROR,
                "The stream was interrupted before the answer was complete.",
                details={"endpoint": stream.endpoint.name},
            )
            status, error_code, http_status = "error", failure.code, failure.http_status
            for frame in render_error(failure):
                yield frame
            finished = True
    finally:
        if not finished and status == "success":
            status, error_code, http_status = (
                "aborted", CLIENT_CLOSED_REQUEST, CLIENT_CLOSED_STATUS,
            )
        try:
            await stream.aclose()
        finally:
            await ctx.finalize(
                usage(),
                ttft_ms=stream.ttft_ms,
                status=status,
                http_status=http_status,
                error_code=error_code,
            )
