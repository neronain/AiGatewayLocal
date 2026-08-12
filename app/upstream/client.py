"""Pooled HTTP client for backend model servers.

One shared AsyncClient for the process lifetime: connection reuse matters a lot
when every request is a long-lived streaming response to the same few hosts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import get_settings
from app.core.errors import ErrorCode, GatewayError
from app.registry.schema import Endpoint, ServerType

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Hop-by-hop and identity headers we must not relay upstream.
_STRIPPED_REQUEST_HEADERS = {
    "host",
    "authorization",
    "x-api-key",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "cookie",
}
_STRIPPED_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "set-cookie",
}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout,
                read=settings.upstream_read_timeout,
                write=settings.upstream_read_timeout,
                pool=settings.upstream_connect_timeout,
            ),
            limits=httpx.Limits(
                max_connections=settings.upstream_max_connections,
                max_keepalive_connections=settings.upstream_max_connections // 2,
            ),
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def upstream_url(endpoint: Endpoint, path: str) -> str:
    """Map a gateway path to the backend path for this server type."""
    base = endpoint.normalized_base_url
    if endpoint.server_type == ServerType.OLLAMA:
        # Ollama exposes the OpenAI-compatible surface under /v1.
        if not path.startswith("/v1"):
            path = "/v1" + path
    return base + path


def upstream_headers(endpoint: Endpoint, incoming: dict[str, str]) -> dict[str, str]:
    """Forward safe client headers; swap in the backend's own credential."""
    headers = {
        k: v for k, v in incoming.items() if k.lower() not in _STRIPPED_REQUEST_HEADERS
    }
    headers["content-type"] = "application/json"
    if endpoint.api_key_env:
        key = os.environ.get(endpoint.api_key_env, "")
        if key:
            headers["authorization"] = f"Bearer {key}"
        else:
            log.warning(
                "endpoint %s declares api_key_env=%s but it is unset",
                endpoint.name,
                endpoint.api_key_env,
            )
    return headers


def sanitize_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        k: v for k, v in headers.items() if k.lower() not in _STRIPPED_RESPONSE_HEADERS
    }


async def post_json(
    endpoint: Endpoint, path: str, payload: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    url = upstream_url(endpoint, path)
    try:
        return await get_client().post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_TIMEOUT,
            "The model server did not respond in time. Please retry.",
            details={"endpoint": endpoint.name},
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            f"Could not reach the model server: {type(exc).__name__}.",
            details={"endpoint": endpoint.name},
        ) from exc


@asynccontextmanager
async def stream_json(
    endpoint: Endpoint, path: str, payload: dict[str, Any], headers: dict[str, str]
) -> AsyncIterator[httpx.Response]:
    url = upstream_url(endpoint, path)
    request = get_client().build_request("POST", url, json=payload, headers=headers)
    try:
        response = await get_client().send(request, stream=True)
    except httpx.TimeoutException as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_TIMEOUT,
            "The model server did not respond in time. Please retry.",
            details={"endpoint": endpoint.name},
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            f"Could not reach the model server: {type(exc).__name__}.",
            details={"endpoint": endpoint.name},
        ) from exc
    try:
        yield response
    finally:
        await response.aclose()


async def read_error_body(response: httpx.Response) -> str:
    try:
        if response.is_stream_consumed:
            return ""
        raw = await response.aread()
        return raw.decode("utf-8", errors="replace")[:2000]
    except Exception:
        return ""


def upstream_error(endpoint: Endpoint, status_code: int, body: str) -> GatewayError:
    """Translate a backend failure into our envelope without leaking internals."""
    if status_code == 404:
        message = (
            "The model is not loaded on the backend server. "
            "Ask an administrator to verify the upstream model name."
        )
    elif status_code in (401, 403):
        message = "The gateway is not authorized to call the model server."
    elif status_code == 429:
        message = "The model server is rate limiting. Please retry shortly."
    elif status_code >= 500:
        message = "The model server returned an internal error."
    else:
        message = "The model server rejected the request."
    return GatewayError(
        ErrorCode.UPSTREAM_ERROR,
        message,
        details={
            "endpoint": endpoint.name,
            "upstream_status": status_code,
            # Truncated backend text: useful for the admin console, harmless to members.
            "upstream_detail": body[:500],
        },
    )
