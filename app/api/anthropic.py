"""Anthropic Messages surface: /v1/messages (FR-25, PRD §8).

This is the endpoint Claude Code talks to. Two paths:

  * the selected endpoint declares `protocols.anthropic: true` -> native forward
  * otherwise -> translate to OpenAI on the way out and back on the way in,
    including the streaming event sequence.

Which path is taken is decided by *tested capability*, never by the model name.
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
    _resolve_model,
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
from app.core.multimodal import profile_anthropic_request
from app.core.rules import resolve_route
from app.core.tokens import TokenUsage, resolve_usage
from app.db.session import get_session, release_connection
from app.registry.schema import Endpoint, ModelDefinition
from app.state import AppState, get_state
from app.upstream import client as upstream
from app.upstream.protocol.anthropic import (
    AnthropicStreamAdapter,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
    wants_thinking,
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
router = APIRouter(tags=["anthropic"])

MESSAGES_PATH = "/v1/messages"
CHAT_PATH = "/v1/chat/completions"


@router.post(MESSAGES_PATH)
async def messages(
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

    model = _resolve_model(state, alias, principal)
    await assert_model_permitted(
        session, principal, alias, state.registry.snapshot.gateway
    )

    # The alias must have the Anthropic surface enabled. Whether that surface is
    # served natively or by translation is a backend detail decided below.
    validate_protocol(model, "anthropic")

    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_anthropic_request(body, policy)

    # เหตุผลของลำดับนี้อยู่ใน app/core/rules.py — Claude Code เป็นลูกค้าหลักของ
    # surface นี้ และเป็นตัวที่ยิงทั้ง context ยาวมากและงานจุกจิกถี่ ๆ พร้อมกัน
    decision = resolve_route(
        state.registry.snapshot, model, profile, "anthropic", body.get("max_tokens")
    )
    if decision.rerouted:
        log.info(
            "routing %s -> %s (%s, request %s)",
            alias, decision.model.alias, decision.reason, request_id,
        )
        model = decision.model

    validate_model_capabilities(model, profile)
    # เพดานคำตอบที่ส่งจริงคิดใหม่ต่อโมเดลที่เสิร์ฟ (ctx.output_cap) — ตรงนี้แค่ปฏิเสธ prompt ยาวเกิน
    validate_context_budget(model, profile, body.get("max_tokens"))

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

    # Prefer a backend that speaks Anthropic natively; otherwise translate over
    # an OpenAI backend. This is a property of the endpoints, not of the alias.
    def _select(target: ModelDefinition, exclude: Collection[str] = ()) -> Endpoint:
        want_native = any(e.enabled and e.protocols.anthropic for e in target.spec.endpoints)
        try:
            return state.router.select(
                target, profile, "anthropic" if want_native else "openai", exclude=exclude
            )
        except GatewayError:
            if not want_native:
                raise
            # native ไม่เหลือ แต่ตัวแปลยังรับได้
            return state.router.select(target, profile, "openai", exclude=exclude)

    # ทุกเครื่องของ alias นี้ล่ม — ลองโมเดลสำรองก่อนตอบ 503
    model, endpoint = select_or_fall_back(
        state, model, profile, "anthropic", _select, request_id
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
        protocol="anthropic",
        requested_max_tokens=body.get("max_tokens"),
        select=_select,
    )
    # Whether to translate is a property of the machine that ends up serving,
    # not of the alias: a request that fails over from a native Anthropic box to
    # an OpenAI-only one has to be rewritten, not merely re-sent. Deciding it
    # here, per attempt, is also what keeps the two in step.
    def build(target: Endpoint) -> _Attempt:
        # อ่านจาก ctx: fallback ระดับโมเดลเปลี่ยนตัวที่ใช้จริงได้ระหว่างทาง
        active = ctx.model
        out_cap = ctx.output_cap()
        if target.protocols.anthropic:
            payload = dict(body)
            payload["model"] = upstream_model_for(active, target)
            payload["max_tokens"] = out_cap
            path, translate = MESSAGES_PATH, False
        else:
            payload = anthropic_to_openai_request(body, upstream_model_for(active, target))
            payload["max_tokens"] = out_cap
            path, translate = CHAT_PATH, True
        return _Attempt(
            payload=payload,
            headers=upstream.upstream_headers(target, dict(request.headers)),
            path=path,
            translate=translate,
            thinking=wants_thinking(body),
        )

    if body.get("stream"):
        return await _stream_messages(request, build, ctx)
    return await _complete_messages(build, ctx)


@dataclass(frozen=True)
class _Attempt:
    payload: dict[str, Any]
    headers: dict[str, str]
    path: str
    translate: bool
    # ผู้เรียกขอ thinking มา — ตัวแปลจะคืนความคิดของโมเดลเป็น block `thinking` ให้
    thinking: bool = False


BuildAttempt = Callable[[Endpoint], _Attempt]


def _plan(build: BuildAttempt) -> lifecycle.Plan:
    def plan(endpoint: Endpoint) -> lifecycle.Call:
        attempt = build(endpoint)
        return lifecycle.Call(
            attempt.path,
            attempt.payload,
            attempt.headers,
            lifecycle.OPENAI if attempt.translate else lifecycle.ANTHROPIC,
            extra=attempt,
        )

    return plan


async def _complete_messages(build: BuildAttempt, ctx: _RequestContext) -> FastJSONResponse:
    alias = ctx.requested_alias
    # จองช่อง · เรียก · สลับเครื่อง/โมเดลสำรอง · ตรวจว่า body ของ 200 ใช้ได้จริง — ดู lifecycle
    call, endpoint, data = await lifecycle.complete(ctx, _plan(build))
    attempt: _Attempt = call.extra
    translate = attempt.translate

    reported = data.get("usage")

    if translate:
        data = openai_to_anthropic_response(data, alias, include_thinking=attempt.thinking)
    else:
        data["model"] = alias

    usage = resolve_usage(ctx.profile, reported, _rate(ctx))
    data["usage"] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
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
            "x-litegate-protocol": "anthropic-native" if not translate else "anthropic-via-openai",
            **({"x-litegate-failed-over": ",".join(sorted(ctx.tried))} if ctx.tried else {}),
        },
    )


async def _stream_messages(
    request: Request, build: BuildAttempt, ctx: _RequestContext
) -> Response:
    # เปิดสายและรอ payload แรก *ก่อน* เริ่มตอบ — ดู lifecycle.open_stream
    stream = await lifecycle.open_stream_for(request, ctx, _plan(build))
    if stream is None:
        return Response(status_code=lifecycle.CLIENT_CLOSED_STATUS)

    alias = ctx.requested_alias
    attempt: _Attempt = stream.call.extra
    translate = attempt.translate
    upstream_usage: dict | None = None
    adapter = (
        AnthropicStreamAdapter(alias, include_thinking=attempt.thinking)
        if translate else None
    )

    def usage() -> TokenUsage:
        return resolve_usage(ctx.profile, upstream_usage, _rate(ctx))

    async def produce() -> AsyncIterator[bytes]:
        nonlocal upstream_usage
        async for event, _data, chunk in stream.payloads():
            if chunk is None:
                continue

            if adapter is None:
                # Native stream: relay, masking the model name.
                if chunk.get("type") == "message_start":
                    message = chunk.get("message")
                    if isinstance(message, dict):
                        message["model"] = alias
                usage_block = _extract_anthropic_usage(chunk)
                if usage_block:
                    upstream_usage = {**(upstream_usage or {}), **usage_block}
                yield format_json_sse(chunk, event=event or chunk.get("type"))
                continue

            if isinstance(chunk.get("usage"), dict):
                upstream_usage = chunk["usage"]
            for ev_name, ev_payload in adapter.handle_chunk(chunk):
                yield format_json_sse(ev_payload, event=ev_name)

        if adapter is not None:
            for ev_name, ev_payload in adapter.finish_events():
                yield format_json_sse(ev_payload, event=ev_name)

    def render_error(exc: GatewayError) -> list[bytes]:
        # event `error` คือวิธีที่ streaming ของ Anthropic บอกว่าคำตอบไม่จบ — SDK ยกเป็น
        # exception แทนที่จะคืน message ครึ่งเดียวที่ดูเหมือนจบปกติ
        return [format_json_sse(exc.to_anthropic(ctx.request_id), event="error")]

    return StreamingResponse(
        lifecycle.relay(ctx, stream, produce, render_error, usage),
        media_type="text/event-stream",
        headers=lifecycle.stream_headers(
            ctx, stream, "anthropic-via-openai" if translate else "anthropic-native"
        ),
    )


def _extract_anthropic_usage(chunk: dict[str, Any]) -> dict[str, int] | None:
    """Usage arrives on message_start (input) and message_delta (output)."""
    if chunk.get("type") == "message_start":
        usage = (chunk.get("message") or {}).get("usage")
        return usage if isinstance(usage, dict) else None
    if chunk.get("type") == "message_delta":
        usage = chunk.get("usage")
        return usage if isinstance(usage, dict) else None
    return None


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Claude Code calls this before long requests. Estimated, never tokenized.

    ประมาณด้วยอัตรา tokenizer ของโมเดลตัวนี้ — ตัวเดียวกับที่ด่าน context จะใช้ตัดสินคำขอจริง
    Claude Code เอาตัวเลขนี้ไปตัดสินว่าจะย่อบทสนทนาเมื่อไร ถ้าสองที่นับไม่เท่ากัน มันจะย่อ
    เร็วเกินไป (เสีย context ที่ยังใช้ได้) หรือช้าเกินไป (ชน 400 ก่อนได้ย่อ)
    """
    body = await _read_json(request)
    alias = body.get("model", "")
    model = _resolve_model(state, alias, principal)
    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_anthropic_request(body, policy)
    usage = resolve_usage(profile, None, model.spec.wide_chars_per_token)
    return {
        "input_tokens": usage.input_tokens,
        "litegate": {
            "text_input_tokens": usage.text_input_tokens,
            "visual_input_tokens": usage.visual_input_tokens,
            "accounting": "estimated",
        },
    }


def native_anthropic_available(endpoint: Endpoint) -> bool:
    return endpoint.protocols.anthropic
