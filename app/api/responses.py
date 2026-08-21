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
from app.core.multimodal import profile_responses_request
from app.core.routing import RETRYABLE_ERRORS, is_retryable_status
from app.core.rules import fallback_models, resolve_route
from app.core.tokens import resolve_usage
from app.db.session import get_session
from app.registry.schema import Endpoint
from app.state import AppState, get_state
from app.upstream import client as upstream
from app.upstream.protocol.responses import (
    ResponsesStreamAdapter,
    openai_to_responses_response,
    responses_to_openai_request,
)
from app.upstream.sse import DONE, format_json_sse, iter_sse_payloads, parse_chunk

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

    model = _resolve_model(state, alias, principal)
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
    effective_max_tokens = validate_context_budget(
        model, profile, body.get("max_output_tokens")
    )

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, alias
    )
    await state.quota.check(principal.user_id, limits)
    # ด่านที่สอง: เพดานของ key ใบนี้เอง (ถ้ามีคนตั้งไว้) · ต้องผ่านทั้งสองด่าน —
    # ถ้าให้ด่านใดด่านหนึ่งชนะ การออก key ใบใหม่จะกลายเป็นวิธีขอโควตาเพิ่ม
    key_limits = await state.quota.resolve_key_limits(session, principal.api_key_id)
    if key_limits is not None:
        await state.quota.check_key(principal.api_key_id, key_limits)

    def _select(target):
        want_native = any(
            e.enabled and e.protocols.responses for e in target.spec.endpoints
        )
        try:
            return state.router.select(
                target, profile, "responses" if want_native else "openai"
            )
        except GatewayError:
            if not want_native:
                raise
            return state.router.select(target, profile, "openai")

    try:
        endpoint = _select(model)
    except GatewayError:
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
        request_id=request_id,
        started=started,
        client_agent=request.headers.get("user-agent", "")[:128],
        protocol="responses",
    )

    def build(target: Endpoint) -> _Attempt:
        active = ctx.model
        out_cap = min(effective_max_tokens, active.spec.limits.max_output_tokens)
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
        return await _stream_response(build, ctx)
    return await _complete_response(build, ctx)


@dataclass(frozen=True)
class _Attempt:
    payload: dict[str, Any]
    headers: dict[str, str]
    path: str
    translate: bool


BuildAttempt = Callable[[Endpoint], _Attempt]


async def _complete_response(build: BuildAttempt, ctx: _RequestContext) -> JSONResponse:
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
        data = openai_to_responses_response(data, alias)
    else:
        data["model"] = alias

    usage = resolve_usage(ctx.profile, _openai_shaped_usage(data.get("usage")))
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


async def _stream_response(build: BuildAttempt, ctx: _RequestContext) -> StreamingResponse:
    async def generator() -> AsyncIterator[bytes]:
        state, alias = ctx.state, ctx.requested_alias
        upstream_usage: dict | None = None
        ttft_ms: int | None = None
        status, error_code, http_status = "success", None, 200
        emitted = False

        try:
            while True:
                endpoint = ctx.endpoint
                attempt = build(endpoint)
                translate = attempt.translate
                adapter = ResponsesStreamAdapter(alias) if translate else None

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
                                    error.to_openai(ctx.request_id), event="error"
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
                                    inner = chunk.get("response")
                                    if isinstance(inner, dict):
                                        inner["model"] = alias
                                        if isinstance(inner.get("usage"), dict):
                                            upstream_usage = _openai_shaped_usage(inner["usage"])
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
                        yield format_json_sse(exc.to_openai(ctx.request_id), event="error")
                        return
                except Exception as exc:
                    log.exception("responses stream failed for request %s", ctx.request_id)
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
