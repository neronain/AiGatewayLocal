"""Console sign-in and self-service (FR-41..FR-45).

Two credentials, two audiences:

  * an **API key** authenticates a program. It lives in a config file, it is
    long-lived, and a member may hold several - one per machine or project.
  * a **console session** authenticates a person. It lives in a cookie, it
    expires in hours, and it is what you use to issue, rename or revoke your
    keys.

The console used to ask for an API key, which meant pasting a production
credential into a browser and left no way back in once that key was revoked.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.auth import Principal, authenticate, generate_api_key, normalise_role
from app.core.errors import ErrorCode, GatewayError
from app.core.passwords import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    PasswordError,
    hash_password,
    issue_session,
    verify_password,
)
from app.db.models import ApiKey, User, utcnow
from app.db.session import get_session
from app.state import AppState, get_state

log = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class SetupRequest(Credentials):
    display_name: str = ""


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    # Secure only over TLS: on a plain-HTTP lab deployment a Secure cookie is
    # simply never sent, which looks like a broken login.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


async def _admin_exists(session: AsyncSession) -> bool:
    result = await session.execute(
        select(func.count()).select_from(User).where(User.role.in_(("admin", "manager")))
    )
    return bool(result.scalar() or 0)


@router.get("/auth/status")
async def auth_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """What the sign-in page needs before anyone has typed anything.

    Unauthenticated on purpose: it reveals only whether the instance has been
    set up, which a first-run screen has to know.
    """
    from app.core.passwords import read_session

    signed_in = None
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        payload = read_session(raw)
        if payload:
            user = await session.get(User, payload.get("sub"))
            if user and user.status == "active" and int(payload.get("epoch", -1)) == int(
                user.session_epoch or 0
            ):
                signed_in = {
                    "user_id": user.id,
                    "username": user.external_id,
                    "display_name": user.display_name,
                    "role": normalise_role(user.role),
                    "must_change_password": bool(user.must_change_password),
                }

    return {"needs_setup": not await _admin_exists(session), "session": signed_in}


@router.post("/auth/setup", status_code=201)
async def setup(
    payload: SetupRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Create the first administrator, and only the first.

    Once any admin or manager exists this is closed permanently, so it cannot be
    used to add a second admin later.
    """
    if await _admin_exists(session):
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            "This instance is already set up. Sign in instead.",
        )
    try:
        digest = hash_password(payload.password)
    except PasswordError as exc:
        raise GatewayError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    user = User(
        external_id=payload.username.strip(),
        display_name=payload.display_name.strip() or payload.username.strip(),
        role="admin",
        password_hash=digest,
        session_epoch=0,
    )
    session.add(user)
    await session.commit()

    _set_session_cookie(response, request, issue_session(user.id, 0))
    log.warning("first administrator created: %s", user.external_id)
    return {"user_id": user.id, "username": user.external_id, "role": "admin"}


@router.post("/auth/login")
async def login(
    payload: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await session.execute(
        select(User).where(User.external_id == payload.username.strip())
    )
    user = result.scalar_one_or_none()

    # One message for every failure: no way to learn which usernames exist.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise GatewayError(ErrorCode.INVALID_API_KEY, "Incorrect username or password.")
    if user.status != "active":
        raise GatewayError(
            ErrorCode.ACCOUNT_DISABLED, "This account is not active. Contact an administrator."
        )

    _set_session_cookie(response, request, issue_session(user.id, int(user.session_epoch or 0)))
    return {
        "user_id": user.id,
        "username": user.external_id,
        "display_name": user.display_name,
        "role": normalise_role(user.role),
        "must_change_password": bool(user.must_change_password),
    }


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"signed_out": True}


class PasswordChange(BaseModel):
    current_password: str = ""
    new_password: str = Field(min_length=1, max_length=512)


@router.post("/auth/password")
async def change_password(
    payload: PasswordChange,
    request: Request,
    response: Response,
    principal: Principal = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Change your own password. Signs out every other session."""
    if principal.via != "session":
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            "Passwords can only be changed from the console, not with an API key.",
        )
    user = await session.get(User, principal.user_id)
    if user is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Account not found.")

    # Skipped only for an account that has never had one (invite flow).
    if user.password_hash and not verify_password(payload.current_password, user.password_hash):
        raise GatewayError(ErrorCode.INVALID_API_KEY, "Current password is incorrect.")

    try:
        user.password_hash = hash_password(payload.new_password)
    except PasswordError as exc:
        raise GatewayError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    user.session_epoch = int(user.session_epoch or 0) + 1
    user.must_change_password = False
    await session.commit()

    # Keep the caller signed in on the session they used to make the change.
    _set_session_cookie(response, request, issue_session(user.id, int(user.session_epoch)))
    return {"changed": True, "other_sessions_signed_out": True}


# ---------------------------------------------------------------------------
# Self-service API keys
# ---------------------------------------------------------------------------
class SelfKeyRequest(BaseModel):
    name: str = Field(default="", max_length=128)
    expires_in_days: int | None = 180


def _key_view(key: ApiKey) -> dict[str, Any]:
    return {
        "id": key.id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked": key.revoked_at is not None,
    }


@router.get("/v1/me/api-keys")
async def list_my_keys(
    principal: Principal = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == principal.user_id).order_by(
            ApiKey.created_at.desc()
        )
    )
    keys = list(result.scalars())
    active = sum(1 for k in keys if k.revoked_at is None)
    return {
        "data": [_key_view(k) for k in keys],
        "active": active,
        "limit": state.settings.max_keys_per_member,
    }


@router.post("/v1/me/api-keys", status_code=201)
async def create_my_key(
    payload: SelfKeyRequest,
    principal: Principal = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Issue yourself a key. Console only, and capped.

    Console only because a leaked key must not be able to mint replacements for
    itself. Capped because keys are the thing nobody ever cleans up, and an
    unbounded list is one nobody can audit.
    """
    if principal.via != "session":
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            "Sign in to the console to issue a key. An API key cannot mint another.",
        )

    result = await session.execute(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.user_id == principal.user_id, ApiKey.revoked_at.is_(None))
    )
    active = int(result.scalar() or 0)
    limit = state.settings.max_keys_per_member
    if active >= limit:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"You already have {active} active keys, the maximum is {limit}. "
            "Revoke one you no longer use.",
            details={"active": active, "limit": limit},
        )

    plaintext, prefix, digest = generate_api_key()
    days = payload.expires_in_days
    api_key = ApiKey(
        user_id=principal.user_id,
        workspace_id=principal.workspace_id,
        name=payload.name.strip() or "console",
        key_prefix=prefix,
        key_hash=digest,
        scopes=[],
        expires_at=(utcnow() + timedelta(days=days)) if days else None,
    )
    session.add(api_key)
    await session.commit()

    return {
        **_key_view(api_key),
        "api_key": plaintext,
        "warning": "Store this key now. It cannot be retrieved again.",
    }


@router.delete("/v1/me/api-keys/{key_id}")
async def revoke_my_key(
    key_id: str,
    principal: Principal = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    api_key = await session.get(ApiKey, key_id)
    # Same answer for "not yours" and "does not exist": no probing other
    # people's key ids.
    if api_key is None or api_key.user_id != principal.user_id:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "API key not found.")
    api_key.revoked_at = utcnow()
    await session.commit()
    return {"id": key_id, "revoked": True}


def bootstrap_settings() -> tuple[str, str]:
    settings = get_settings()
    return settings.admin_user, settings.admin_password
