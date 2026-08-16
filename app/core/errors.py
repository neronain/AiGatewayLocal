"""Gateway error taxonomy (PRD v1.3 §10).

Every rejection the gateway originates uses this envelope, so clients can branch
on a stable machine code instead of parsing prose:

    {"error": {"code": "...", "message": "...", "param": null,
               "type": "invalid_request_error", "request_id": "..."}}

The shape is compatible with the OpenAI error object, so OpenAI SDKs surface
`err.code` unchanged. The Anthropic surface re-wraps it in Anthropic's shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import status


class ErrorCode:
    # --- authentication / authorization (401, 403) ---
    MISSING_API_KEY = "MISSING_API_KEY"
    INVALID_API_KEY = "INVALID_API_KEY"
    API_KEY_REVOKED = "API_KEY_REVOKED"
    API_KEY_EXPIRED = "API_KEY_EXPIRED"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    MODEL_NOT_PERMITTED = "MODEL_NOT_PERMITTED"

    # --- request validation (400, 404, 413, 415) ---
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    MODEL_DISABLED = "MODEL_DISABLED"
    MODEL_CAPABILITY_NOT_SUPPORTED = "MODEL_CAPABILITY_NOT_SUPPORTED"
    PROTOCOL_NOT_SUPPORTED = "PROTOCOL_NOT_SUPPORTED"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CONTENT_BLOCK = "INVALID_CONTENT_BLOCK"
    IMAGE_TYPE_NOT_ALLOWED = "IMAGE_TYPE_NOT_ALLOWED"
    IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
    TOO_MANY_IMAGES = "TOO_MANY_IMAGES"
    REMOTE_IMAGE_URL_DISABLED = "REMOTE_IMAGE_URL_DISABLED"
    CONTEXT_LENGTH_EXCEEDED = "CONTEXT_LENGTH_EXCEEDED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_NOT_PUBLISHED = "TOOL_NOT_PUBLISHED"

    # --- policy (429) ---
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    CONCURRENCY_LIMIT_EXCEEDED = "CONCURRENCY_LIMIT_EXCEEDED"

    # --- upstream / infrastructure (502, 503, 504) ---
    NO_HEALTHY_ENDPOINT = "NO_HEALTHY_ENDPOINT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# code -> (http status, openai "type")
_ERROR_META: dict[str, tuple[int, str]] = {
    ErrorCode.MISSING_API_KEY: (status.HTTP_401_UNAUTHORIZED, "authentication_error"),
    ErrorCode.INVALID_API_KEY: (status.HTTP_401_UNAUTHORIZED, "authentication_error"),
    ErrorCode.API_KEY_REVOKED: (status.HTTP_401_UNAUTHORIZED, "authentication_error"),
    ErrorCode.API_KEY_EXPIRED: (status.HTTP_401_UNAUTHORIZED, "authentication_error"),
    ErrorCode.ACCOUNT_DISABLED: (status.HTTP_403_FORBIDDEN, "permission_error"),
    ErrorCode.INSUFFICIENT_SCOPE: (status.HTTP_403_FORBIDDEN, "permission_error"),
    ErrorCode.MODEL_NOT_PERMITTED: (status.HTTP_403_FORBIDDEN, "permission_error"),
    ErrorCode.MODEL_NOT_FOUND: (status.HTTP_404_NOT_FOUND, "invalid_request_error"),
    ErrorCode.MODEL_DISABLED: (status.HTTP_404_NOT_FOUND, "invalid_request_error"),
    ErrorCode.TOOL_NOT_FOUND: (status.HTTP_404_NOT_FOUND, "invalid_request_error"),
    ErrorCode.TOOL_NOT_PUBLISHED: (status.HTTP_400_BAD_REQUEST, "invalid_request_error"),
    ErrorCode.MODEL_CAPABILITY_NOT_SUPPORTED: (
        status.HTTP_400_BAD_REQUEST,
        "invalid_request_error",
    ),
    ErrorCode.PROTOCOL_NOT_SUPPORTED: (
        status.HTTP_400_BAD_REQUEST,
        "invalid_request_error",
    ),
    ErrorCode.INVALID_REQUEST: (status.HTTP_400_BAD_REQUEST, "invalid_request_error"),
    ErrorCode.INVALID_CONTENT_BLOCK: (
        status.HTTP_400_BAD_REQUEST,
        "invalid_request_error",
    ),
    ErrorCode.IMAGE_TYPE_NOT_ALLOWED: (
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "invalid_request_error",
    ),
    ErrorCode.IMAGE_TOO_LARGE: (413, "invalid_request_error"),  # Content Too Large
    ErrorCode.TOO_MANY_IMAGES: (status.HTTP_400_BAD_REQUEST, "invalid_request_error"),
    ErrorCode.REMOTE_IMAGE_URL_DISABLED: (
        status.HTTP_400_BAD_REQUEST,
        "invalid_request_error",
    ),
    ErrorCode.CONTEXT_LENGTH_EXCEEDED: (
        status.HTTP_400_BAD_REQUEST,
        "invalid_request_error",
    ),
    ErrorCode.QUOTA_EXCEEDED: (status.HTTP_429_TOO_MANY_REQUESTS, "rate_limit_error"),
    ErrorCode.RATE_LIMIT_EXCEEDED: (
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limit_error",
    ),
    ErrorCode.CONCURRENCY_LIMIT_EXCEEDED: (
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limit_error",
    ),
    ErrorCode.NO_HEALTHY_ENDPOINT: (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "api_error",
    ),
    ErrorCode.UPSTREAM_ERROR: (status.HTTP_502_BAD_GATEWAY, "api_error"),
    ErrorCode.UPSTREAM_TIMEOUT: (status.HTTP_504_GATEWAY_TIMEOUT, "api_error"),
    ErrorCode.UPSTREAM_UNAVAILABLE: (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "api_error",
    ),
    ErrorCode.INTERNAL_ERROR: (
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "api_error",
    ),
}


class GatewayError(Exception):
    """Raised anywhere in the request path; rendered by the global handler."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        param: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param
        self.details = details or {}
        self.retry_after = retry_after

    @property
    def http_status(self) -> int:
        return _ERROR_META.get(self.code, (500, "api_error"))[0]

    @property
    def error_type(self) -> str:
        return _ERROR_META.get(self.code, (500, "api_error"))[1]

    def to_openai(self, request_id: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "type": self.error_type,
            "param": self.param,
        }
        if self.details:
            error["details"] = self.details
        if request_id:
            error["request_id"] = request_id
        return {"error": error}

    def to_anthropic(self, request_id: str | None = None) -> dict[str, Any]:
        anthropic_type = {
            "authentication_error": "authentication_error",
            "permission_error": "permission_error",
            "invalid_request_error": "invalid_request_error",
            "rate_limit_error": "rate_limit_error",
            "api_error": "api_error",
        }.get(self.error_type, "api_error")
        error: dict[str, Any] = {
            "type": anthropic_type,
            "message": self.message,
            "code": self.code,
        }
        if request_id:
            error["request_id"] = request_id
        return {"type": "error", "error": error}


def capability_error(alias: str, capability: str, hint: str = "") -> GatewayError:
    """PRD §4: reject before touching the backend, with an actionable message."""
    message = f"Model '{alias}' does not support {capability}."
    if hint:
        message = f"{message} {hint}"
    return GatewayError(
        ErrorCode.MODEL_CAPABILITY_NOT_SUPPORTED,
        message,
        details={"model": alias, "required_capability": capability},
    )
