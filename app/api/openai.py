"""OpenAI-compatible surface: /v1/models, /v1/chat/completions (FR-30..FR-35).

The pipeline, in the order the PRD specifies (§15):

    authenticate -> workspace policy -> resolve alias -> parse content blocks
    -> validate model capability -> validate vision policy -> context budget
    -> quota -> select compatible healthy endpoint -> forward -> record usage
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import usage as usage_mod
from app.core.auth import (
    Principal,
    assert_model_permitted,
    authenticate,
    permitted_aliases,
)
from app.core.capability import (
    compatibility_badges,
    upstream_model_for,
    validate_context_budget,
    validate_model_capabilities,
    validate_protocol,
)
from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import RequestProfile, profile_openai_request
from app.core.quota import Consumption
from app.core.routing import RETRYABLE_ERRORS, is_retryable_status
from app.core.tokens import TokenUsage, resolve_usage
from app.db.session import get_session
from app.registry.schema import Endpoint, ModelDefinition
from app.state import AppState, get_state
from app.upstream import client as upstream
from app.upstream.sse import DONE, format_sse, iter_sse_payloads, parse_chunk

log = logging.getLogger(__name__)
router = APIRouter(tags=["openai"])

CHAT_PATH = "/v1/chat/completions"

# Addresses one request to one backend. Called once per attempt, because the
# upstream model name and the API key belong to the machine, not the request.
BuildRequest = Callable[[Endpoint], tuple[dict[str, Any], dict[str, str]]]


@router.get("/v1/models")
async def list_models(
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """OpenAI-shaped catalogue. Members only ever see the alias (PRD §6).

    Filtered by the same rule that gates the call. Listing a model that would be
    refused is worse than not listing it: the client offers it, the person picks
    it, and the error arrives after they have written their prompt.
    """
    snapshot = state.registry.snapshot
    permission = await permitted_aliases(session, principal, snapshot.gateway)
    data = []
    for model in snapshot.visible_to(principal.role):
        if not permission.allows(model.alias):
            continue
        entry: dict[str, Any] = {
            "id": model.alias,
            "object": "model",
            "created": 0,
            "owned_by": "litegate",
            # Non-standard but harmless extras that OpenAI SDKs pass through.
            "display_name": model.metadata.display_name,
            "description": model.metadata.description,
            "purpose": [p.value for p in model.spec.purpose],
            "capabilities": model.spec.capabilities.model_dump(),
            "modalities": {
                "input": [m.value for m in model.spec.modalities.input],
                "output": [m.value for m in model.spec.modalities.output],
            },
            "context_window": model.spec.limits.context_tokens,
            "max_output_tokens": model.spec.limits.max_output_tokens,
            "badges": compatibility_badges(model),
        }
        if principal.is_admin:
            entry["upstream_model"] = model.spec.upstream_model
            entry["endpoints"] = [e.name for e in model.spec.endpoints]
        data.append(entry)
    return {"object": "list", "data": data}


@router.post(CHAT_PATH)
async def chat_completions(
    request: Request,
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
):
    return await run_chat(request, await _read_json(request), principal, state, session)


async def run_chat(
    request: Request,
    body: dict[str, Any],
    principal: Principal,
    state: AppState,
    session: AsyncSession,
):
    """The chat pipeline, callable with a body from anywhere.

    The assistant builds its own request and needs the identical treatment -
    capability gate, vision policy, context budget, quota, routing, usage. Going
    through this rather than reimplementing it is what stops the assistant from
    becoming a way around the rules that apply to everyone else.
    """
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    started = time.perf_counter()

    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "'model' is required.", param="model"
        )

    model = _resolve_model(state, alias, principal)
    await assert_model_permitted(
        session, principal, alias, state.registry.snapshot.gateway
    )
    validate_protocol(model, "openai")

    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_openai_request(body, policy)
    validate_model_capabilities(model, profile)

    effective_max_tokens = validate_context_budget(
        model, profile, body.get("max_tokens") or body.get("max_completion_tokens")
    )

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, alias
    )
    await state.quota.check(principal.user_id, limits)

    endpoint = state.router.select(model, profile, "openai")

    # Rebuilt per attempt rather than once: the upstream model name and the API
    # key are properties of the machine, so a request that fails over has to be
    # re-addressed, not merely re-sent.
    def build(target: Endpoint) -> tuple[dict[str, Any], dict[str, str]]:
        payload = dict(body)
        payload["model"] = upstream_model_for(model, target)
        if body.get("max_tokens") or body.get("max_completion_tokens"):
            payload.pop("max_completion_tokens", None)
            payload["max_tokens"] = effective_max_tokens
        return payload, upstream.upstream_headers(target, dict(request.headers))

    client_agent = request.headers.get("user-agent", "")[:128]

    context = _RequestContext(
        state=state,
        principal=principal,
        model=model,
        endpoint=endpoint,
        profile=profile,
        limits_window=limits.window,
        request_id=request_id,
        started=started,
        client_agent=client_agent,
        protocol="openai",
    )

    if body.get("stream"):
        return await _stream_chat(build, context)
    return await _complete_chat(build, context)


# ---------------------------------------------------------------------------
# Shared request context + bookkeeping
# ---------------------------------------------------------------------------
class _RequestContext:
    def __init__(
        self,
        *,
        state: AppState,
        principal: Principal,
        model: ModelDefinition,
        endpoint: Endpoint,
        profile: RequestProfile,
        limits_window: str,
        request_id: str,
        started: float,
        client_agent: str,
        protocol: str,
    ) -> None:
        self.state = state
        self.principal = principal
        self.model = model
        self.endpoint = endpoint
        self.profile = profile
        self.limits_window = limits_window
        self.request_id = request_id
        self.started = started
        self.client_agent = client_agent
        self.protocol = protocol
        # Which backends this request has already burned. Not a count: the same
        # machine must never be handed the request twice, and it stays healthy
        # for two more strikes after the first failure.
        self.tried: set[str] = set()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def another_endpoint(self) -> Endpoint | None:
        """A backend for this alias that has not been tried yet, or None.

        None covers both "there is only one machine" and "we have been through
        all of them", which the caller treats the same way: stop and report the
        failure it already has.
        """
        self.tried.add(self.endpoint.name)
        try:
            return self.state.router.select(
                self.model, self.profile, self.protocol, exclude=self.tried
            )
        except GatewayError:
            return None

    def retarget(self, endpoint: Endpoint) -> None:
        log.info(
            "failing over %s: %s -> %s (request %s)",
            self.model.alias, self.endpoint.name, endpoint.name, self.request_id,
        )
        self.endpoint = endpoint

    async def finalize(
        self,
        usage: TokenUsage,
        *,
        ttft_ms: int | None = None,
        status: str = "success",
        http_status: int = 200,
        error_code: str | None = None,
    ) -> None:
        """Record usage + quota consumption exactly once per request."""
        record = usage_mod.build_record(
            request_id=self.request_id,
            principal=self.principal,
            model_alias=self.model.alias,
            protocol=self.protocol,
            profile=self.profile,
            usage=usage,
            endpoint_name=self.endpoint.name,
            stream=self.profile.requires_streaming,
            latency_ms=self.elapsed_ms,
            ttft_ms=ttft_ms,
            status=status,
            http_status=http_status,
            error_code=error_code,
            client_agent=self.client_agent,
        )
        await self.state.usage.submit(record)
        await self.state.quota.record(
            self.principal.user_id,
            self.limits_window,
            Consumption(
                requests=1,
                text_input_tokens=usage.text_input_tokens,
                visual_input_tokens=usage.visual_input_tokens,
                output_tokens=usage.output_tokens,
                images=self.profile.image_count,
            ),
        )


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------
async def _complete_chat(build: BuildRequest, ctx: _RequestContext) -> JSONResponse:
    """Ask a backend, and if that one is unwell, ask the next one.

    Nothing has reached the caller yet at this point, so a retry is invisible to
    them - which is the whole difference between one machine going down and one
    conversation breaking.
    """
    state, alias = ctx.state, ctx.model.alias
    while True:
        endpoint = ctx.endpoint
        payload, headers = build(endpoint)
        state.router.acquire(alias, endpoint)
        try:
            response = await upstream.post_json(endpoint, CHAT_PATH, payload, headers)
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
            body = response.text[:2000]
            state.router.report_failure(alias, endpoint, f"HTTP {response.status_code}")
            if is_retryable_status(response.status_code) and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            error = upstream.upstream_error(endpoint, response.status_code, body)
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

    # The member asked for the alias; never leak the upstream repository name.
    data["model"] = alias
    usage = resolve_usage(ctx.profile, data.get("usage"))
    _augment_usage_payload(data, usage)
    await ctx.finalize(usage)

    return JSONResponse(
        content=data,
        headers={
            "x-request-id": ctx.request_id,
            "x-litegate-model": alias,
            # Legacy alias, one release only: scripts and dashboards still
            # read x-edullm-model.
            "x-edullm-model": alias,
            "x-litegate-endpoint": endpoint.name,
            # Names the machines that were tried and failed before this one, so
            # a slow reply has a visible reason rather than an unexplained one.
            **({"x-litegate-failed-over": ",".join(sorted(ctx.tried))} if ctx.tried else {}),
        },
    )


def _augment_usage_payload(data: dict[str, Any], usage: TokenUsage) -> None:
    """Expose the visual split without breaking the OpenAI usage shape."""
    existing = data.get("usage")
    if not isinstance(existing, dict):
        existing = {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
        }
    existing["litegate"] = {
        "text_input_tokens": usage.text_input_tokens,
        "visual_input_tokens": usage.visual_input_tokens,
        "accounting": usage.accounting,
    }
    data["usage"] = existing


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
async def _stream_chat(build: BuildRequest, ctx: _RequestContext) -> StreamingResponse:
    async def generator() -> AsyncIterator[bytes]:
        state, alias = ctx.state, ctx.model.alias
        upstream_usage: dict | None = None
        ttft_ms: int | None = None
        status, error_code, http_status = "success", None, 200
        # Once a chunk has left for the caller, failing over would replay the
        # answer from the top and they would read it twice. Before that, the
        # switch is invisible - so this flag is the whole retry policy here.
        emitted = False

        try:
            while True:
                endpoint = ctx.endpoint
                payload, headers = build(endpoint)
                # Ask for a final usage chunk so accounting stays authoritative.
                # If the caller did not want it, it is stripped before
                # forwarding so the shape matches what they asked for.
                client_wants_usage = bool(
                    (payload.get("stream_options") or {}).get("include_usage")
                )
                payload["stream_options"] = {
                    **(payload.get("stream_options") or {}),
                    "include_usage": True,
                }

                retry: Endpoint | None = None
                state.router.acquire(alias, endpoint)
                try:
                    async with upstream.stream_json(
                        endpoint, CHAT_PATH, payload, headers
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
                                yield format_sse(json.dumps(error.to_openai(ctx.request_id)))
                                yield format_sse(DONE)
                                return
                        else:
                            state.router.report_success(alias, endpoint)
                            async for _event, data in iter_sse_payloads(response.aiter_lines()):
                                if data.strip() == DONE:
                                    continue
                                chunk = parse_chunk(data)
                                if chunk is None:
                                    emitted = True
                                    yield format_sse(data)
                                    continue

                                if ttft_ms is None:
                                    ttft_ms = ctx.elapsed_ms

                                if isinstance(chunk.get("usage"), dict):
                                    upstream_usage = chunk["usage"]
                                    if not client_wants_usage and not chunk.get("choices"):
                                        continue  # usage-only chunk nobody asked for

                                chunk["model"] = alias
                                emitted = True
                                yield format_sse(json.dumps(chunk, ensure_ascii=False))

                            yield format_sse(DONE)
                            return

                except GatewayError as exc:
                    state.router.report_failure(alias, endpoint, exc.message)
                    if not emitted and exc.code in RETRYABLE_ERRORS:
                        retry = ctx.another_endpoint()
                    if retry is None:
                        status, error_code, http_status = "error", exc.code, exc.http_status
                        yield format_sse(json.dumps(exc.to_openai(ctx.request_id)))
                        yield format_sse(DONE)
                        return
                except Exception as exc:  # client disconnect, backend reset, ...
                    log.exception("stream failed for request %s", ctx.request_id)
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
            "x-accel-buffering": "no",  # nginx must not buffer SSE
            "x-request-id": ctx.request_id,
            "x-litegate-model": ctx.model.alias,
            "x-edullm-model": ctx.model.alias,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "Request body must be valid JSON."
        ) from exc
    if not isinstance(body, dict):
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Request body must be a JSON object.")
    return body


def _resolve_model(state: AppState, alias: str, principal: Principal) -> ModelDefinition:
    snapshot = state.registry.snapshot
    model = snapshot.models.get(alias)
    if model is None:
        available = sorted(m.alias for m in snapshot.visible_to(principal.role))
        raise GatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Model '{alias}' does not exist. Available models: {', '.join(available)}.",
            param="model",
            details={"available_models": available},
        )
    if not model.spec.enabled:
        raise GatewayError(
            ErrorCode.MODEL_DISABLED,
            f"Model '{alias}' is currently disabled.",
            param="model",
        )
    if model not in snapshot.visible_to(principal.role):
        raise GatewayError(
            ErrorCode.MODEL_NOT_PERMITTED,
            f"Model '{alias}' is not available for your account.",
            param="model",
        )
    return model
