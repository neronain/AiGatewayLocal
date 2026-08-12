"""API-key authentication and the request Principal.

Key format:  lg_sk_<43 url-safe base64 chars>   (256 bits of entropy)

Keys issued before the rename start with `edu_sk_` and keep working: a key is
verified by HMAC over the whole string, so the prefix is a label for a human
reading a key list, not part of the check.

Because the secret is high-entropy random (not a human password), a single
HMAC-SHA256 with a server-side pepper is the correct verification primitive:
it is constant-time comparable, unforgeable without the pepper, and fast enough
to run on every request. A slow KDF would only add latency to the hot path.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import ErrorCode, GatewayError
from app.db.models import ApiKey, User, WorkspaceModel, utcnow
from app.db.session import get_session

log = logging.getLogger(__name__)

KEY_PREFIX = "lg_sk_"

# Roles recorded before the rename. Rows are not rewritten on upgrade, so a
# manager stored as "instructor" must keep their privileges.
LEGACY_ROLES = {"student": "member", "instructor": "manager"}


def normalise_role(role: str) -> str:
    return LEGACY_ROLES.get(role, role)
PREFIX_LEN = 12


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, key_prefix, key_hash). Plaintext is never stored."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, plaintext[:PREFIX_LEN], hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    pepper = get_settings().api_key_pepper.encode()
    return hmac.new(pepper, plaintext.encode(), hashlib.sha256).hexdigest()


@dataclass
class Principal:
    """Everything the request path needs to know about the caller."""

    user_id: str
    external_id: str
    role: str
    display_name: str
    api_key_id: str
    workspace_id: str | None
    scopes: list[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_manager(self) -> bool:
        return self.role in {"admin", "manager"}

    def require_scope(self, scope: str) -> None:
        if self.is_admin or not self.scopes or scope in self.scopes:
            return
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            f"This API key is not authorized for scope '{scope}'.",
        )


def extract_bearer_token(request: Request) -> str:
    """Accept both OpenAI (`Authorization: Bearer`) and Anthropic (`x-api-key`)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    x_api_key = request.headers.get("x-api-key", "").strip()
    if x_api_key:
        return x_api_key
    raise GatewayError(
        ErrorCode.MISSING_API_KEY,
        "No API key provided. Send 'Authorization: Bearer <key>' or 'x-api-key: <key>'.",
    )


async def authenticate(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """FastAPI dependency: resolve the caller or raise a GatewayError."""
    token = extract_bearer_token(request)
    digest = hash_api_key(token)

    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        # Same message for unknown vs malformed: no oracle for key probing.
        raise GatewayError(ErrorCode.INVALID_API_KEY, "Invalid API key.")

    now = utcnow()
    if api_key.revoked_at is not None:
        raise GatewayError(ErrorCode.API_KEY_REVOKED, "This API key has been revoked.")
    if api_key.expires_at is not None and _aware(api_key.expires_at) < now:
        raise GatewayError(ErrorCode.API_KEY_EXPIRED, "This API key has expired.")

    user = await session.get(User, api_key.user_id)
    if user is None or user.status != "active":
        raise GatewayError(
            ErrorCode.ACCOUNT_DISABLED, "This account is not active. Contact your manager."
        )

    # Best-effort last-used stamp; never fail a request over telemetry.
    try:
        api_key.last_used_at = now
        await session.commit()
    except Exception:
        await session.rollback()
        log.warning("could not update last_used_at for key %s", api_key.id)

    return Principal(
        user_id=user.id,
        external_id=user.external_id,
        role=normalise_role(user.role),
        display_name=user.display_name,
        api_key_id=api_key.id,
        workspace_id=api_key.workspace_id,
        scopes=list(api_key.scopes or []),
    )


async def require_admin(principal: Principal = Depends(authenticate)) -> Principal:
    if not principal.is_admin:
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE, "Administrator privileges are required."
        )
    return principal


async def require_manager(principal: Principal = Depends(authenticate)) -> Principal:
    if not principal.is_manager:
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE, "Manager privileges are required."
        )
    return principal


async def assert_model_permitted(
    session: AsyncSession, principal: Principal, alias: str
) -> None:
    """Workspace policy gate (PRD §15 step 1).

    Admins and managers bypass. A key bound to a workspace may only use aliases
    that workspace enables. An unbound member key is allowed any member-visible
    model - visibility is enforced separately by the registry.
    """
    if principal.is_manager:
        return
    if principal.workspace_id is None:
        return

    result = await session.execute(
        select(WorkspaceModel).where(
            WorkspaceModel.workspace_id == principal.workspace_id,
            WorkspaceModel.model_alias == alias,
        )
    )
    row = result.scalar_one_or_none()
    if row is None or not row.enabled:
        raise GatewayError(
            ErrorCode.MODEL_NOT_PERMITTED,
            f"Model '{alias}' is not enabled for your workspace.",
            details={"model": alias},
        )


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; normalize before comparing."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
