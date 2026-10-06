"""OpenAI Responses surface: /v1/responses — the API Codex speaks.

Same two paths as the Anthropic surface, decided per attempt by tested capability
rather than by model name:

  * the selected endpoint declares `protocols.responses: true` -> native forward
  * otherwise -> translate to chat completions on the way out and back on the way
    in, including the typed event sequence Codex reads while streaming.

Everything above the translation is shared with the other surfaces on purpose:
the same alias resolution, permission check, capability gate, context budget,
quota, routing rules and failover. A second way in must not become a second set
of rules.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Collection
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import lifecycle
from app.api.openai import (
    _read_json,
    _RequestContext,
    resolve_model,
    select_or_fall_back,
)
from app.core.auth import Principal, assert_model_permitted, authenticate
from app.core.capability import (
    upstream_model_for,
    validate_context_budget,
    validate_model_capabilities,
    validate_protocol,
)
from app.core.errors import ErrorCode, GatewayError
from app.core.jsonio import FastJSONResponse
from app.core.multimodal import profile_responses_request
from app.core.rules import resolve_route
from app.core.tokens import OutputMeter, TokenUsage, resolve_usage
from app.db.session import get_session, release_connection
from app.registry.schema import Endpoint, ModelDefinition
from app.state import AppState, get_state
from app.upstream import client as upstream
from app.upstream.protocol.responses import (
    ResponsesStreamAdapter,
    openai_to_responses_response,
    responses_to_openai_request,
)
from app.upstream.sse import format_json_sse


def _rate(ctx) -> float | None:
    """อัตราอักขระนอก ASCII ต่อ token ของโมเดลที่ *เสิร์ฟจริง*

    อ่านจาก ctx ตอนนั้น ไม่ใช่จำไว้ล่วงหน้า — fallback ระดับโมเดลเปลี่ยน ctx.model
    ระหว่างคำขอได้ ถ้าจำค่าของตัวแรกไว้ ยอดที่บันทึกจะเป็นของโมเดลที่ไม่ได้รัน
    """
    model = getattr(ctx, "model", None)
    spec = getattr(model, "spec", None)
    return getattr(spec, "wide_chars_per_token", None)

log = logging.getLogger(__name__)
router = APIRouter(tags=["responses"])

RESPONSES_PATH = "/v1/responses"
CHAT_PATH = "/v1/chat/completions"


@router.post(RESPONSES_PATH)
async def create_response(
    request: Request,
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    started = time.perf_counter()
    body = await _read_json(request)

    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "'model' is required.", param="model")

    # Codex keeps conversation state on the server with previous_response_id.
    # LiteGate stores no prompts and no responses by design (PRD §12), so there is
    # nothing to continue from - saying so is better than answering with the tail
    # of a conversation whose head we silently dropped.
    if body.get("previous_response_id"):
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "'previous_response_id' is not supported: this gateway keeps no "
            "conversation state. Send the full input each turn.",
            param="previous_response_id",
        )

    model = await resolve_model(state, session, alias, principal)
    await assert_model_permitted(
        session, principal, alias, state.registry.snapshot.gateway
    )
    validate_protocol(model, "responses")

    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_responses_request(body, policy)

    decision = resolve_route(
        state.registry.snapshot, model, profile, "responses", body.get("max_output_tokens")
    )
    if decision.rerouted:
        log.info(
            "routing %s -> %s (%s, request %s)",
            alias, decision.model.alias, decision.reason, request_id,
        )
        model = decision.model

    validate_model_capabilities(model, profile)
    # เพดานคำตอบที่ส่งจริงคิดใหม่ต่อโมเดลที่เสิร์ฟ (ctx.output_cap) — ตรงนี้แค่ปฏิเสธ prompt ยาวเกิน
    validate_context_budget(model, profile, body.get("max_output_tokens"))

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, alias
    )
    await state.quota.check(principal.user_id, limits)
    # ด่านที่สอง: เพดานของ key ใบนี้เอง (ถ้ามีคนตั้งไว้) · ต้องผ่านทั้งสองด่าน —
    # ถ้าให้ด่านใดด่านหนึ่งชนะ การออก key ใบใหม่จะกลายเป็นวิธีขอโควตาเพิ่ม
    key_limits = await state.quota.resolve_key_limits(session, principal.api_key_id)
    if key_limits is not None:
        await state.quota.check_key(principal.api_key_id, key_limits)

    # The request's last read. Everything past this point - choosing an
    # endpoint, the upstream call, the stream itself, and the usage and quota
    # bookkeeping in ctx.finalize - runs without the request session, so give
    # the connection back now instead of when FastAPI tears the dependency down,
    # which for a stream is after the last SSE byte, minutes from here.
    # See app/db/session.py: release_connection.
    await release_connection(session)

    def _select(target: ModelDefinition, exclude: Collection[str] = ()) -> Endpoint:
        want_native = any(
            e.enabled and e.protocols.responses for e in target.spec.endpoints
        )
        try:
            return state.router.select(
                target, profile, "responses" if want_native else "openai", exclude=exclude
            )
        except GatewayError:
            if not want_native:
                raise
            return state.router.select(target, profile, "openai", exclude=exclude)

    model, endpoint = select_or_fall_back(
        state, model, profile, "responses", _select, request_id
    )

    ctx = _RequestContext(
        state=state,
        principal=principal,
        model=model,
        endpoint=endpoint,
        requested_alias=alias,
        profile=profile,
        limits_window=limits.window,
        rate_limited=limits.rate_limited,
        key_window=key_limits.window if key_limits else "",
        key_rate_limited=bool(key_limits and key_limits.rate_limited),
        request_id=request_id,
        client_request_id=getattr(request.state, "client_request_id", None),
        started=started,
        client_agent=request.headers.get("user-agent", "")[:128],
        protocol="responses",
        requested_max_tokens=body.get("max_output_tokens"),
        select=_select,
    )

    def build(target: Endpoint) -> _Attempt:
        active = ctx.model
        out_cap = ctx.output_cap()
        if target.protocols.responses:
            payload = dict(body)
            payload["model"] = upstream_model_for(active, target)
            payload["max_output_tokens"] = out_cap
            path, translate = RESPONSES_PATH, False
        else:
            payload = responses_to_openai_request(body, upstream_model_for(active, target))
            payload["max_tokens"] = out_cap
            path, translate = CHAT_PATH, True
        return _Attempt(
            payload=payload,
            headers=upstream.upstream_headers(target, dict(request.headers)),
            path=path,
            translate=translate,
        )

    if body.get("stream"):
        return await _stream_response(request, build, ctx)
    return await _complete_response(build, ctx)


@dataclass(frozen=True)
class _Attempt:
    payload: dict[str, Any]
    headers: dict[str, str]
    path: str
    translate: bool


BuildAttempt = Callable[[Endpoint], _Attempt]


def _plan(build: BuildAttempt) -> lifecycle.Plan:
    def plan(endpoint: Endpoint) -> lifecycle.Call:
        attempt = build(endpoint)
        return lifecycle.Call(
            attempt.path,
            attempt.payload,
            attempt.headers,
            lifecycle.OPENAI if attempt.translate else lifecycle.RESPONSES,
            extra=attempt,
        )

    return plan


# รหัสใน `response.failed` ที่ client ของ Responses (Codex) รู้จัก
_FAILURE_CODES = {
    ErrorCode.CONTEXT_LENGTH_EXCEEDED: "context_length_exceeded",
    ErrorCode.CONCURRENCY_LIMIT_EXCEEDED: "rate_limit_exceeded",
    ErrorCode.RATE_LIMIT_EXCEEDED: "rate_limit_exceeded",
    ErrorCode.QUOTA_EXCEEDED: "insufficient_quota",
}


async def _complete_response(build: BuildAttempt, ctx: _RequestContext) -> FastJSONResponse:
    alias = ctx.requested_alias
    # จองช่อง · เรียก · สลับเครื่อง/โมเดลสำรอง · ตรวจว่า body ของ 200 ใช้ได้จริง — ดู lifecycle
    call, endpoint, data = await lifecycle.complete(ctx, _plan(build))
    translate = call.extra.translate

    # นับจากคำตอบของ backend ก่อนแปล — ใช้เมื่อมันไม่รายงาน usage มา
    relayed = OutputMeter()
    lifecycle.meter_for(call.dialect)(relayed, data)
    reported = _openai_shaped_usage(data.get("usage"))

    if translate:
        data = openai_to_responses_response(data, alias)
    else:
        data["model"] = alias

    usage = resolve_usage(ctx.profile, reported, _rate(ctx), relayed=relayed)
    data["usage"] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "litegate": {
            "text_input_tokens": usage.text_input_tokens,
            "visual_input_tokens": usage.visual_input_tokens,
            "accounting": usage.accounting,
        },
    }
    await ctx.finalize(usage)

    return FastJSONResponse(
        content=data,
        headers={
            "x-request-id": ctx.request_id,
            "x-litegate-model": alias,
            # ตัวที่ *รันจริง* — ต่างจาก x-litegate-model เมื่อกฎ routing เปลี่ยนเส้นทาง
            # (coding -> coding-long เพราะคำขอยาวเกิน) · สัญญากับสมาชิกยังเหมือนเดิม
            # คือขอ alias ไหนได้ alias นั้น แต่เวลาไล่ปัญหาต้องรู้ว่าใครตอบ ไม่งั้นตัวเลข
            # เร็ว/ช้าที่วัดได้จะถูกโยงไปผิดโมเดล
            "x-litegate-served-by": ctx.model.alias,
            "x-litegate-endpoint": endpoint.name,
            "x-litegate-protocol": (
                "responses-via-openai" if translate else "responses-native"
            ),
            **({"x-litegate-failed-over": ",".join(sorted(ctx.tried))} if ctx.tried else {}),
        },
    )


def _openai_shaped_usage(usage: Any) -> dict[str, Any] | None:
    """resolve_usage speaks the chat-completions field names."""
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }


async def _stream_response(
    request: Request, build: BuildAttempt, ctx: _RequestContext
) -> Response:
    # เปิดสายและรอ payload แรก *ก่อน* เริ่มตอบ — ดู lifecycle.open_stream
    stream = await lifecycle.open_stream_for(request, ctx, _plan(build))
    if stream is None:
        return Response(status_code=lifecycle.CLIENT_CLOSED_STATUS)

    alias = ctx.requested_alias
    translate = stream.call.extra.translate
    upstream_usage: dict | None = None
    relayed = OutputMeter()
    meter = lifecycle.meter_for(stream.call.dialect)
    adapter = ResponsesStreamAdapter(alias)
    # native: จำ id กับลำดับของ backend ไว้ เผื่อต้องปิดเองด้วย response.failed
    native_id: str | None = None
    native_seq = 0

    def usage() -> TokenUsage:
        return resolve_usage(ctx.profile, upstream_usage, _rate(ctx), relayed=relayed)

    async def produce() -> AsyncIterator[bytes]:
        nonlocal upstream_usage, native_id, native_seq
        async for event, _data, chunk in stream.payloads():
            if chunk is None:
                continue
            meter(relayed, chunk)

            if not translate:
                # Native stream: relay, masking the model name.
                inner = chunk.get("response")
                if isinstance(inner, dict):
                    inner["model"] = alias
                    native_id = inner.get("id") or native_id
                    if isinstance(inner.get("usage"), dict):
                        upstream_usage = _openai_shaped_usage(inner["usage"])
                if isinstance(chunk.get("sequence_number"), int):
                    native_seq = chunk["sequence_number"] + 1
                yield format_json_sse(chunk, event=event or chunk.get("type"))
                continue

            if isinstance(chunk.get("usage"), dict):
                upstream_usage = chunk["usage"]
            for ev_name, ev_payload in adapter.handle_chunk(chunk):
                yield format_json_sse(ev_payload, event=ev_name)

        if translate:
            final = usage()
            for ev_name, ev_payload in adapter.finish_events(
                input_tokens=final.input_tokens, output_tokens=final.output_tokens
            ):
                yield format_json_sse(ev_payload, event=ev_name)

    def render_error(exc: GatewayError) -> list[bytes]:
        if not translate:
            adapter.resume(native_id, native_seq)
        final = usage()
        return [
            format_json_sse(ev_payload, event=ev_name)
            for ev_name, ev_payload in adapter.fail_events(
                _FAILURE_CODES.get(exc.code, "server_error"),
                exc.message,
                input_tokens=final.input_tokens,
                output_tokens=final.output_tokens,
            )
        ]

    return StreamingResponse(
        lifecycle.relay(ctx, stream, produce, render_error, usage),
        media_type="text/event-stream",
        headers=lifecycle.stream_headers(
            ctx, stream, "responses-via-openai" if translate else "responses-native"
        ),
    )
