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
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openai import _read_json, _RequestContext, _resolve_model
from app.core.auth import Principal, assert_model_permitted, authenticate
from app.core.capability import (
    validate_context_budget,
    validate_model_capabilities,
    validate_protocol,
)
from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import profile_anthropic_request
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
    await assert_model_permitted(session, principal, alias)

    # The alias must have the Anthropic surface enabled. Whether that surface is
    # served natively or by translation is a backend detail decided below.
    validate_protocol(model, "anthropic")

    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_anthropic_request(body, policy)
    validate_model_capabilities(model, profile)
    effective_max_tokens = validate_context_budget(model, profile, body.get("max_tokens"))

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.course_id, alias
    )
    await state.quota.check(principal.user_id, limits)

    # Prefer a backend that speaks Anthropic natively; otherwise translate over
    # an OpenAI backend. This is a property of the endpoints, not of the alias.
    native = any(
        e.enabled and e.protocols.anthropic for e in model.spec.endpoints
    )
    try:
        endpoint = state.router.select(model, profile, "anthropic" if native else "openai")
    except GatewayError:
        if not native:
            raise
        native = False
        endpoint = state.router.select(model, profile, "openai")

    ctx = _RequestContext(
        state=state,
        principal=principal,
        model=model,
        endpoint=endpoint,
        profile=profile,
        limits_window=limits.window,
        request_id=request_id,
        started=started,
        client_agent=request.headers.get("user-agent", "")[:128],
        protocol="anthropic",
    )
    headers = upstream.upstream_headers(endpoint, dict(request.headers))
    stream = bool(body.get("stream"))

    if native:
        payload = dict(body)
        payload["model"] = model.spec.upstream_model
        payload["max_tokens"] = effective_max_tokens
        path = MESSAGES_PATH
        translate = False
    else:
        payload = anthropic_to_openai_request(body, model.spec.upstream_model)
        payload["max_tokens"] = effective_max_tokens
        path = CHAT_PATH
        translate = True

    if stream:
        return await _stream_messages(payload, headers, ctx, path, translate)
    return await _complete_messages(payload, headers, ctx, path, translate)


async def _complete_messages(
    payload: dict[str, Any],
    headers: dict[str, str],
    ctx: _RequestContext,
    path: str,
    translate: bool,
) -> JSONResponse:
    state, endpoint, alias = ctx.state, ctx.endpoint, ctx.model.alias
    state.router.acquire(alias, endpoint)
    try:
        response = await upstream.post_json(endpoint, path, payload, headers)
    except GatewayError as exc:
        state.router.report_failure(alias, endpoint, exc.message)
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
        error = upstream.upstream_error(endpoint, response.status_code, response.text[:2000])
        await ctx.finalize(
            resolve_usage(ctx.profile, None),
            status="error",
            http_status=error.http_status,
            error_code=error.code,
        )
        raise error

    state.router.report_success(alias, endpoint)
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
        "edullm": {
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
            "x-edullm-model": alias,
            "x-edullm-endpoint": endpoint.name,
            "x-edullm-protocol": "anthropic-native" if not translate else "anthropic-via-openai",
        },
    )


async def _stream_messages(
    payload: dict[str, Any],
    headers: dict[str, str],
    ctx: _RequestContext,
    path: str,
    translate: bool,
) -> StreamingResponse:
    async def generator() -> AsyncIterator[bytes]:
        state, endpoint, alias = ctx.state, ctx.endpoint, ctx.model.alias
        adapter = AnthropicStreamAdapter(alias) if translate else None
        upstream_usage: dict | None = None
        ttft_ms: int | None = None
        status, error_code, http_status = "success", None, 200

        state.router.acquire(alias, endpoint)
        try:
            async with upstream.stream_json(endpoint, path, payload, headers) as response:
                if response.status_code >= 400:
                    body = await upstream.read_error_body(response)
                    state.router.report_failure(alias, endpoint, f"HTTP {response.status_code}")
                    error = upstream.upstream_error(endpoint, response.status_code, body)
                    status, error_code, http_status = "error", error.code, error.http_status
                    yield format_json_sse(error.to_anthropic(ctx.request_id), event="error")
                    return

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
                        # Native Anthropic stream: relay, masking the model name.
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

                if translate and adapter is not None:
                    for ev_name, ev_payload in adapter.finish_events():
                        yield format_json_sse(ev_payload, event=ev_name)

        except GatewayError as exc:
            state.router.report_failure(alias, endpoint, exc.message)
            status, error_code, http_status = "error", exc.code, exc.http_status
            yield format_json_sse(exc.to_anthropic(ctx.request_id), event="error")
        except Exception as exc:
            log.exception("anthropic stream failed for request %s", ctx.request_id)
            state.router.report_failure(alias, endpoint, str(exc))
            status, error_code, http_status = "aborted", ErrorCode.UPSTREAM_ERROR, 502
        finally:
            state.router.release(alias, endpoint)
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
            "x-edullm-model": ctx.model.alias,
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
        "edullm": {
            "text_input_tokens": usage.text_input_tokens,
            "visual_input_tokens": usage.visual_input_tokens,
            "accounting": "estimated",
        },
    }


def native_anthropic_available(endpoint: Endpoint) -> bool:
    return endpoint.protocols.anthropic
