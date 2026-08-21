"""Anthropic Messages surface: /v1/messages (FR-25, PRD §8).

This is the endpoint Claude Code talks to. Two paths:

  * the selected endpoint declares `protocols.anthropic: true` -> native forward
  * otherwise -> translate to OpenAI on the way out and back on the way in,
    including the streaming event sequence.

Which path is taken is decided by *tested capability*, never by the model name.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openai import _read_json, _RequestContext, _resolve_model
from app.core.auth import Principal, assert_model_permitted, authenticate
from app.core.capability import (
    upstream_model_for,
    validate_context_budget,
    validate_model_capabilities,
    validate_protocol,
)
from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import profile_anthropic_request
from app.core.routing import RETRYABLE_ERRORS, is_retryable_status
from app.core.rules import fallback_models, resolve_route
from app.core.tokens import resolve_usage
from app.db.session import get_session
from app.registry.schema import Endpoint
from app.state import AppState, get_state
from app.upstream import client as upstream
from app.upstream.protocol.anthropic import (
    AnthropicStreamAdapter,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from app.upstream.sse import DONE, format_json_sse, iter_sse_payloads, parse_chunk

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
    effective_max_tokens = validate_context_budget(model, profile, body.get("max_tokens"))

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, alias
    )
    await state.quota.check(principal.user_id, limits)
    # ด่านที่สอง: เพดานของ key ใบนี้เอง (ถ้ามีคนตั้งไว้) · ต้องผ่านทั้งสองด่าน —
    # ถ้าให้ด่านใดด่านหนึ่งชนะ การออก key ใบใหม่จะกลายเป็นวิธีขอโควตาเพิ่ม
    key_limits = await state.quota.resolve_key_limits(session, principal.api_key_id)
    if key_limits is not None:
        await state.quota.check_key(principal.api_key_id, key_limits)

    # Prefer a backend that speaks Anthropic natively; otherwise translate over
    # an OpenAI backend. This is a property of the endpoints, not of the alias.
    def _select(target):
        want_native = any(e.enabled and e.protocols.anthropic for e in target.spec.endpoints)
        try:
            return state.router.select(target, profile, "anthropic" if want_native else "openai")
        except GatewayError:
            if not want_native:
                raise
            # native ไม่เหลือ แต่ตัวแปลยังรับได้
            return state.router.select(target, profile, "openai")

    try:
        endpoint = _select(model)
    except GatewayError:
        # ทุกเครื่องของ alias นี้ล่ม — ลองโมเดลสำรองก่อนตอบ 503
        for candidate in fallback_models(state.registry.snapshot, model):
            try:
                endpoint = _select(candidate)
            except GatewayError:
                continue
            log.warning(
                "no endpoint for %s; falling back to %s (request %s)",
                model.alias, candidate.alias, request_id,
            )
            model = candidate
            break
        else:
            raise

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
        started=started,
        client_agent=request.headers.get("user-agent", "")[:128],
        protocol="anthropic",
    )
    # Whether to translate is a property of the machine that ends up serving,
    # not of the alias: a request that fails over from a native Anthropic box to
    # an OpenAI-only one has to be rewritten, not merely re-sent. Deciding it
    # here, per attempt, is also what keeps the two in step.
    def build(target: Endpoint) -> _Attempt:
        # อ่านจาก ctx: fallback ระดับโมเดลเปลี่ยนตัวที่ใช้จริงได้ระหว่างทาง
        active = ctx.model
        out_cap = min(effective_max_tokens, active.spec.limits.max_output_tokens)
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
        )

    if body.get("stream"):
        return await _stream_messages(build, ctx)
    return await _complete_messages(build, ctx)


@dataclass(frozen=True)
class _Attempt:
    payload: dict[str, Any]
    headers: dict[str, str]
    path: str
    translate: bool


BuildAttempt = Callable[[Endpoint], _Attempt]


async def _complete_messages(build: BuildAttempt, ctx: _RequestContext) -> JSONResponse:
    state, alias = ctx.state, ctx.requested_alias
    while True:
        endpoint = ctx.endpoint
        attempt = build(endpoint)
        translate = attempt.translate
        state.router.acquire(alias, endpoint)
        try:
            response = await upstream.post_json(
                endpoint, attempt.path, attempt.payload, attempt.headers
            )
        except GatewayError as exc:
            state.router.report_failure(alias, endpoint, exc.message)
            if exc.code in RETRYABLE_ERRORS and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            await ctx.finalize(
                resolve_usage(ctx.profile, None),
                status="error",
                http_status=exc.http_status,
                error_code=exc.code,
            )
            raise
        finally:
            state.router.release(alias, endpoint)

        if response.status_code >= 400:
            state.router.report_failure(alias, endpoint, f"HTTP {response.status_code}")
            if is_retryable_status(response.status_code) and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            error = upstream.upstream_error(
                endpoint, response.status_code, response.text[:2000]
            )
            await ctx.finalize(
                resolve_usage(ctx.profile, None),
                status="error",
                http_status=error.http_status,
                error_code=error.code,
            )
            raise error

        state.router.report_success(alias, endpoint)
        break

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_ERROR, "The model server returned a malformed response."
        ) from exc

    if translate:
        data = openai_to_anthropic_response(data, alias)
    else:
        data["model"] = alias

    usage = resolve_usage(ctx.profile, data.get("usage"))
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

    return JSONResponse(
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


async def _stream_messages(build: BuildAttempt, ctx: _RequestContext) -> StreamingResponse:
    async def generator() -> AsyncIterator[bytes]:
        state, alias = ctx.state, ctx.requested_alias
        upstream_usage: dict | None = None
        ttft_ms: int | None = None
        status, error_code, http_status = "success", None, 200
        # After the first event reaches the caller a retry would replay the
        # answer from the beginning, so the switch is only available before it.
        emitted = False

        try:
            while True:
                endpoint = ctx.endpoint
                attempt = build(endpoint)
                translate = attempt.translate
                adapter = AnthropicStreamAdapter(alias) if translate else None

                retry: Endpoint | None = None
                state.router.acquire(alias, endpoint)
                try:
                    async with upstream.stream_json(
                        endpoint, attempt.path, attempt.payload, attempt.headers
                    ) as response:
                        if response.status_code >= 400:
                            body = await upstream.read_error_body(response)
                            state.router.report_failure(
                                alias, endpoint, f"HTTP {response.status_code}"
                            )
                            if not emitted and is_retryable_status(response.status_code):
                                retry = ctx.another_endpoint()
                            if retry is None:
                                error = upstream.upstream_error(
                                    endpoint, response.status_code, body
                                )
                                status = "error"
                                error_code, http_status = error.code, error.http_status
                                yield format_json_sse(
                                    error.to_anthropic(ctx.request_id), event="error"
                                )
                                return
                        else:
                            state.router.report_success(alias, endpoint)

                            async for event, data in iter_sse_payloads(response.aiter_lines()):
                                if data.strip() == DONE:
                                    continue
                                chunk = parse_chunk(data)
                                if chunk is None:
                                    continue
                                if ttft_ms is None:
                                    ttft_ms = ctx.elapsed_ms

                                if not translate:
                                    # Native stream: relay, masking the model name.
                                    if chunk.get("type") == "message_start":
                                        message = chunk.get("message")
                                        if isinstance(message, dict):
                                            message["model"] = alias
                                    usage_block = _extract_anthropic_usage(chunk)
                                    if usage_block:
                                        upstream_usage = {**(upstream_usage or {}), **usage_block}
                                    emitted = True
                                    yield format_json_sse(
                                        chunk, event=event or chunk.get("type")
                                    )
                                    continue

                                if isinstance(chunk.get("usage"), dict):
                                    upstream_usage = chunk["usage"]
                                for ev_name, ev_payload in adapter.handle_chunk(chunk):
                                    emitted = True
                                    yield format_json_sse(ev_payload, event=ev_name)

                            if translate and adapter is not None:
                                for ev_name, ev_payload in adapter.finish_events():
                                    yield format_json_sse(ev_payload, event=ev_name)
                            return

                except GatewayError as exc:
                    state.router.report_failure(alias, endpoint, exc.message)
                    if not emitted and exc.code in RETRYABLE_ERRORS:
                        retry = ctx.another_endpoint()
                    if retry is None:
                        status, error_code, http_status = "error", exc.code, exc.http_status
                        yield format_json_sse(exc.to_anthropic(ctx.request_id), event="error")
                        return
                except Exception as exc:
                    log.exception("anthropic stream failed for request %s", ctx.request_id)
                    state.router.report_failure(alias, endpoint, str(exc))
                    status, error_code, http_status = "aborted", ErrorCode.UPSTREAM_ERROR, 502
                    return
                finally:
                    state.router.release(alias, endpoint)

                ctx.retarget(retry)
        finally:
            usage = resolve_usage(ctx.profile, upstream_usage)
            await ctx.finalize(
                usage,
                ttft_ms=ttft_ms,
                status=status,
                http_status=http_status,
                error_code=error_code,
            )

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
            "x-request-id": ctx.request_id,
            "x-litegate-model": ctx.requested_alias,
            # ตัวที่ *รันจริง* — ต่างจาก x-litegate-model เมื่อกฎ routing เปลี่ยนเส้นทาง
            # (coding -> coding-long เพราะคำขอยาวเกิน) · สัญญากับสมาชิกยังเหมือนเดิม
            # คือขอ alias ไหนได้ alias นั้น แต่เวลาไล่ปัญหาต้องรู้ว่าใครตอบ ไม่งั้นตัวเลข
            # เร็ว/ช้าที่วัดได้จะถูกโยงไปผิดโมเดล
            "x-litegate-served-by": ctx.model.alias,
        },
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
    """Claude Code calls this before long requests. Estimated, never tokenized."""
    body = await _read_json(request)
    alias = body.get("model", "")
    model = _resolve_model(state, alias, principal)
    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_anthropic_request(body, policy)
    usage = resolve_usage(profile, None)
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
