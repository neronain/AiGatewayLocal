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
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import ErrorCode, GatewayError
from app.db.models import ApiKey, Membership, User, WorkspaceModel, utcnow
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
    # alias ที่ key ใบนี้ระบุไว้เอง · ว่าง = ไม่จำกัดเพิ่ม (ดู assert_model_permitted)
    key_models: list[str] = field(default_factory=list)
    # "key" for a program, "session" for a signed-in human. Self-service actions
    # that mint credentials require a session: a leaked key must not be able to
    # mint more keys for itself.
    via: str = "key"

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
    """Resolve the caller from an API key or a console session.

    Programs send a key; the console sends a cookie. Both end up as the same
    Principal, so every route downstream is written once.
    """
    from_session = await _principal_from_session(request, session)
    if from_session is not None:
        return from_session

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
        key_models=list(api_key.models or []),
        via="key",
    )



async def _principal_from_session(
    request: Request, session: AsyncSession
) -> Principal | None:
    """The browser's cookie, if it carries a session this server still honours."""
    from app.core.passwords import read_session, read_session_cookie

    raw = read_session_cookie(request.cookies, request.url.scheme == "https")
    if not raw:
        return None
    payload = read_session(raw)
    if payload is None:
        return None

    user = await session.get(User, payload.get("sub"))
    if user is None or user.status != "active":
        return None
    # A password change bumps session_epoch, which retires every token issued
    # before it without needing a session table.
    if int(payload.get("epoch", -1)) != int(user.session_epoch or 0):
        return None

    return Principal(
        user_id=user.id,
        external_id=user.external_id,
        role=normalise_role(user.role),
        display_name=user.display_name,
        api_key_id="",
        workspace_id=None,
        scopes=[],
        via="session",
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


@dataclass(frozen=True)
class Permission:
    """What this caller may call, and the sentence explaining why.

    `aliases is None` means nothing narrows them - not "nothing is allowed",
    which is the reading that would lock out every key issued before workspaces
    were used at all.
    """

    aliases: set[str] | None
    reason: str = ""

    def allows(self, alias: str) -> bool:
        return self.aliases is None or alias in self.aliases


UNRESTRICTED = Permission(aliases=None)


async def permitted_aliases(
    session: AsyncSession, principal: Principal, gateway=None
) -> Permission:
    """The one place that decides which models a caller may use.

    Three things can narrow it, and each only ever narrows:

      1. the workspace the key was bound to when it was issued
      2. otherwise, the workspaces its owner belongs to (union across them)
      3. the alias list written on the key itself

    Membership was bookkeeping until v1.5 - it recorded who was in which class
    and granted nothing - so (2) is gated on `membership_grants_models`, and a
    deployment with keys already in circulation should look at
    `scripts/access_change_report.py` before switching it on.

    Union, not intersection, in (2): adding somebody to another class must not
    take access away from them, which is the opposite of what "add to group"
    means to everyone who says it out loud.
    """
    scope: set[str] | None = None
    reason = ""

    if principal.workspace_id is not None:
        scope = await _workspace_models(session, [principal.workspace_id])
        reason = "the workspace this key was issued for"
    elif not principal.is_admin and (gateway is None or gateway.membership_grants_models):
        # Managers are scoped like members: someone who looks after CS101 should
        # not be handing out ART200's models. Admins run the gateway itself and
        # stay unscoped - the alternative is adding them to every workspace,
        # which is a chore with no end and no security value.
        scope = await _models_via_membership(session, principal.user_id)
        if scope is not None:
            reason = "the workspaces you belong to"

    # The list on the key applies to everyone, admins included. The workspace
    # rules above are about who you are; this one is about what the person
    # issuing the key meant it for - a key made for one script should stay
    # limited even if its owner is later promoted.
    if principal.key_models:
        on_key = set(principal.key_models)
        scope = on_key if scope is None else scope & on_key
        reason = (
            f"{reason}, and the list on this key" if reason else "the model list on this key"
        )

    return Permission(aliases=scope, reason=reason)


async def managed_workspaces(
    session: AsyncSession, principal: Principal
) -> set[str] | None:
    """The workspaces this person administers. `None` means all of them.

    A manager manages the classes they are in and nothing else: someone who
    looks after CS101 has no business reading ART200's usage or issuing its
    keys. Admins run the gateway and are not scoped.

    Note the default runs the other way from `permitted_aliases`: a manager in
    no workspace manages nothing, where a member in no workspace may call
    everything. The reason is different in each case. Model access defaults open
    so that a deployment which never adopted workspaces keeps working; there is
    no equivalent history on the admin plane, and defaulting it open would mean
    that promoting somebody to manager silently hands them the whole institution.
    """
    if principal.is_admin:
        return None
    rows = await session.execute(
        select(Membership.workspace_id).where(Membership.user_id == principal.user_id)
    )
    return {row[0] for row in rows}


async def users_in_workspaces(
    session: AsyncSession, workspaces: set[str]
) -> set[str]:
    """Everyone in any of these workspaces."""
    if not workspaces:
        return set()
    rows = await session.execute(
        select(Membership.user_id).where(Membership.workspace_id.in_(workspaces))
    )
    return {row[0] for row in rows}


async def _models_via_membership(session: AsyncSession, user_id: str) -> set[str] | None:
    """One join rather than "which groups" followed by "which models".

    This runs on every request that is not workspace-bound, so the difference
    between one query and two is paid by every call the gateway serves.

    None means the person is in no group at all, which is not the same as being
    in groups that allow nothing: the first is unrestricted, the second is a
    deliberate empty allow-list.
    """
    rows = await session.execute(
        select(Membership.workspace_id, WorkspaceModel.model_alias)
        .outerjoin(
            WorkspaceModel,
            (WorkspaceModel.workspace_id == Membership.workspace_id)
            & (WorkspaceModel.enabled.is_(True)),
        )
        .where(Membership.user_id == user_id)
    )
    pairs = rows.all()
    if not pairs:
        return None
    return {alias for _, alias in pairs if alias is not None}


async def _workspace_models(session: AsyncSession, workspaces: list[str]) -> set[str]:
    rows = await session.execute(
        select(WorkspaceModel.model_alias).where(
            WorkspaceModel.workspace_id.in_(workspaces),
            WorkspaceModel.enabled.is_(True),
        )
    )
    return {row[0] for row in rows}


async def assert_model_permitted(
    session: AsyncSession, principal: Principal, alias: str, gateway=None
) -> None:
    """Workspace policy gate (PRD §15 step 1)."""
    permission = await permitted_aliases(session, principal, gateway)
    if permission.allows(alias):
        return

    allowed = sorted(permission.aliases or [])
    raise GatewayError(
        ErrorCode.MODEL_NOT_PERMITTED,
        f"'{alias}' is not available to you. Allowed by {permission.reason}: "
        + (", ".join(allowed) if allowed else "nothing yet — ask your manager."),
        details={"model": alias, "allowed": allowed, "reason": permission.reason},
    )


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; normalize before comparing."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
