"""Admin plane: users, workspaces, keys, quota, registry, usage (FR-10..FR-19)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import assistant_fit, lmds
from app.core.auth import (
    Principal,
    extract_bearer_token,
    generate_api_key,
    managed_workspaces,
    permitted_aliases,
    require_admin,
    require_manager,
    users_in_workspaces,
)
from app.core.capability import compatibility_badges, upstream_model_for
from app.core.errors import ErrorCode, GatewayError
from app.core.modeltest import (
    ModelTestSuite,
    probe_backend,
    resolve_commands,
    suggest_tool_parser,
)
from app.core.passwords import read_session_cookie
from app.db.models import (
    ASSISTANT_MODEL_KEY,
    AccessGroup,
    ApiKey,
    AuditLog,
    GatewaySetting,
    Membership,
    ModelCompatibility,
    ModelRecord,
    ModelTestRun,
    QuotaPolicy,
    UsageLog,
    User,
    Workspace,
    WorkspaceAccessGroup,
    WorkspaceModel,
    utcnow,
)
from app.db.session import get_session, session_scope
from app.registry.writer import (
    delete_model,
    model_path,
    registry_writable,
    render_yaml,
    set_enabled_in_file,
    set_field_in_file,
    validate_definition,
    write_model,
)
from app.state import AppState, get_state

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Parser names travel into a command on another machine. The pattern is as
# narrow as it can be while still covering every real name.
_PARSER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
TOKEN_KEY_SETTING = lmds.TOKEN_KEY


# ---------------------------------------------------------------------------
# Manager scope
# ---------------------------------------------------------------------------
# A manager administers the workspaces they belong to. Every route below that
# a manager can reach narrows to those; an admin passes through untouched.
async def _scope(session: AsyncSession, actor: Principal) -> set[str] | None:
    """Workspaces this actor may act on, or None for "all of them"."""
    return await managed_workspaces(session, actor)


async def _assert_owns(session: AsyncSession, actor: Principal, workspace_id: str) -> None:
    scope = await _scope(session, actor)
    if scope is None or workspace_id in scope:
        return
    raise GatewayError(
        ErrorCode.INSUFFICIENT_SCOPE,
        "You can only manage workspaces you belong to. Ask an administrator to "
        "add you to this one."
        if scope
        else "You are not in any workspace yet, so there is nothing to manage. "
        "An administrator can add you to one.",
    )


async def _assert_may_grant(
    session: AsyncSession, actor: Principal, aliases: list[str], state: AppState
) -> None:
    """Nobody hands out access they do not have."""
    if actor.is_admin or not aliases:
        return
    permission = await permitted_aliases(
        session, actor, state.registry.snapshot.gateway
    )
    beyond = sorted(a for a in aliases if not permission.allows(a))
    if beyond:
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            f"You cannot grant models you cannot use yourself: {', '.join(beyond)}.",
            details={"models": beyond},
        )


async def _assert_may_read_model(
    session: AsyncSession, actor: Principal, alias: str, state: AppState
) -> None:
    """Test results describe a model. Reading them follows the same scope as
    using it, so a manager does not learn about the fleet through the back."""
    if actor.is_admin:
        return
    permission = await permitted_aliases(
        session, actor, state.registry.snapshot.gateway
    )
    if not permission.allows(alias):
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"Unknown model '{alias}'.")


async def _visible_users(session: AsyncSession, actor: Principal) -> set[str] | None:
    """User ids this actor may see, or None for everyone."""
    scope = await _scope(session, actor)
    if scope is None:
        return None
    # Their own record is always theirs to see; otherwise a manager in no
    # workspace cannot even find themselves in the list.
    return await users_in_workspaces(session, scope) | {actor.user_id}


async def audit(
    session: AsyncSession,
    request: Request,
    actor: Principal,
    action: str,
    target_type: str = "",
    target_id: str = "",
    payload: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload or {},
            ip=request.client.host if request.client else "",
        )
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class UserIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    display_name: str = ""
    email: str | None = None
    role: str = "member"
    status: str = "active"


# role ที่ระบบใช้ตัดสินสิทธิ์จริง — `Principal.is_admin` เทียบ == "admin" ตรง ๆ
# ค่าที่พิมพ์ผิดอย่าง "adminn" หรือ "Admin" จึงไม่พังตอนบันทึก แต่ทำให้คนคนนั้น
# กลายเป็น member เงียบ ๆ แล้วไม่มีอะไรบอก · legacy alias ยังรับไว้เพราะข้อมูลเก่ามี
ROLES = ("member", "manager", "admin")


def _valid_role(value: str) -> str:
    role = str(value or "").strip()
    if role not in ROLES:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"role must be one of {', '.join(ROLES)} (got {value!r}).",
        )
    return role


async def _would_remove_last_admin(session: AsyncSession, user: User, new_role: str) -> bool:
    """เปลี่ยน role นี้แล้วจะไม่เหลือ admin เลยไหม

    ไม่มี admin = ไม่มีใครออก key ใหม่ ตั้ง quota หรือแก้ registry ได้อีก และไม่มี
    ทางกลับผ่านหน้าเว็บด้วย ต้องไปแก้ในฐานข้อมูลเอง
    """
    if user.role != "admin" or new_role == "admin":
        return False
    result = await session.execute(
        select(func.count()).select_from(User).where(User.role == "admin")
    )
    return int(result.scalar() or 0) <= 1


@router.post("/users", status_code=201)
async def create_user(
    payload: UserIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload.role = _valid_role(payload.role)
    existing = await session.execute(
        select(User).where(User.external_id == payload.external_id)
    )
    if existing.scalar_one_or_none():
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"A user with external_id '{payload.external_id}' already exists.",
        )
    user = User(**payload.model_dump())
    session.add(user)
    await audit(session, request, actor, "user.create", "user", payload.external_id)
    await session.commit()
    return _user_dict(user)


@router.get("/users")
async def list_users(
    role: str | None = None,
    limit: int = Query(100, le=1000),
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    if role:
        stmt = stmt.where(User.role == role)
    visible = await _visible_users(session, actor)
    if visible is not None:
        stmt = stmt.where(User.id.in_(visible))
    result = await session.execute(stmt)
    users = list(result.scalars())

    # ดึง membership ทั้งหมดในคำสั่งเดียว ไม่ใช่ต่อคน — และไม่แตะ user.memberships
    # แบบ lazy เพราะ session เป็น async การโหลดตอนอ่านจะระเบิดกลางทาง
    joined: dict[str, list[str]] = {}
    if users:
        rows = await session.execute(
            select(Membership.user_id, Workspace.code)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(Membership.user_id.in_([u.id for u in users]))
        )
        for user_id, code in rows:
            joined.setdefault(user_id, []).append(code)

    return {"data": [{**_user_dict(u), "workspaces": sorted(joined.get(u.id, []))}
                     for u in users]}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user = await session.get(User, user_id)
    if user is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "User not found.")
    if "role" in payload:
        new_role = _valid_role(payload["role"])
        if await _would_remove_last_admin(session, user, new_role):
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                "This is the only administrator. Give someone else the admin role "
                "first — with none, nobody can issue keys or change settings, and "
                "there is no way back through the console.",
            )
        payload = {**payload, "role": new_role}
    for field_name in ("display_name", "email", "role", "status"):
        if field_name in payload:
            setattr(user, field_name, payload[field_name])
    await audit(session, request, actor, "user.update", "user", user_id, payload)
    await session.commit()
    return _user_dict(user)


def _user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "external_id": user.external_id,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------
class WorkspaceIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str
    term: str = ""


@router.post("/workspaces", status_code=201)
async def create_workspace(
    payload: WorkspaceIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    existing = await session.execute(select(Workspace).where(Workspace.code == payload.code))
    if existing.scalar_one_or_none():
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, f"Workspace '{payload.code}' already exists."
        )
    workspace = Workspace(**payload.model_dump())
    session.add(workspace)
    await audit(session, request, actor, "workspace.create", "workspace", payload.code)
    await session.commit()
    return {
        "id": workspace.id,
        "code": workspace.code,
        "name": workspace.name,
        "term": workspace.term,
    }


# ---------------------------------------------------------------------------
# Access groups — a named bundle of aliases, handed out whole
# ---------------------------------------------------------------------------
class AccessGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    models: list[str] = Field(default_factory=list)
    enabled: bool = True


async def _known_aliases(state: AppState, models: list[str]) -> None:
    unknown = [a for a in models if a not in state.registry.snapshot.models]
    if unknown:
        raise GatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Unknown model alias(es): {', '.join(unknown)}.",
            details={"known_models": sorted(state.registry.snapshot.models)},
        )


def _group_dict(group: AccessGroup, used_by: int = 0) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "models": list(group.models or []),
        "enabled": group.enabled,
        "used_by": used_by,
    }


@router.post("/access-groups", status_code=201)
async def create_access_group(
    payload: AccessGroupIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Name a set of models once so it can be handed out whole.

    Only an admin defines a bundle. A manager may hand out bundles they can use
    themselves, but inventing one is how you would otherwise grant yourself a
    model — the bundle would be the loophole rather than the shortcut.
    """
    existing = await session.execute(
        select(AccessGroup).where(AccessGroup.name == payload.name)
    )
    if existing.scalar_one_or_none():
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, f"An access group named '{payload.name}' already exists."
        )
    await _known_aliases(state, payload.models)

    group = AccessGroup(**payload.model_dump())
    session.add(group)
    await audit(session, request, actor, "access_group.create", "access_group",
                payload.name, {"models": payload.models})
    await session.commit()
    return _group_dict(group)


@router.get("/access-groups")
async def list_access_groups(
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    groups = list((await session.execute(
        select(AccessGroup).order_by(AccessGroup.name)
    )).scalars())

    used: dict[str, int] = {}
    if groups:
        rows = await session.execute(
            select(WorkspaceAccessGroup.access_group_id, func.count())
            .group_by(WorkspaceAccessGroup.access_group_id)
        )
        used = {gid: count for gid, count in rows}
    return {"data": [_group_dict(g, used.get(g.id, 0)) for g in groups]}


@router.patch("/access-groups/{group_id}")
async def update_access_group(
    group_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Editing the bundle changes what every workspace holding it may call.

    That is the point of a bundle, and also the thing to be careful about: the
    response says how many workspaces just changed.
    """
    group = await session.get(AccessGroup, group_id)
    if group is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Access group not found.")

    if "models" in payload:
        models = [str(a) for a in payload["models"]]
        await _known_aliases(state, models)
        group.models = models
    for field_name in ("name", "description", "enabled"):
        if field_name in payload:
            setattr(group, field_name, payload[field_name])

    used = await session.execute(
        select(func.count()).select_from(WorkspaceAccessGroup).where(
            WorkspaceAccessGroup.access_group_id == group_id
        )
    )
    await audit(session, request, actor, "access_group.update", "access_group",
                group.name, payload)
    await session.commit()
    return _group_dict(group, int(used.scalar() or 0))


@router.delete("/access-groups/{group_id}")
async def delete_access_group(
    group_id: str,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Deleting a bundle takes its models away from every workspace holding it,
    so it is refused while anyone still holds it. Disable it instead if that is
    what you meant — same effect, and reversible."""
    group = await session.get(AccessGroup, group_id)
    if group is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Access group not found.")

    used = await session.execute(
        select(func.count()).select_from(WorkspaceAccessGroup).where(
            WorkspaceAccessGroup.access_group_id == group_id
        )
    )
    holders = int(used.scalar() or 0)
    if holders:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"'{group.name}' is still given to {holders} workspace(s). Take it away "
            "from them first, or disable it — which stops it granting anything "
            "without losing the list.",
            details={"used_by": holders},
        )

    await session.delete(group)
    await audit(session, request, actor, "access_group.delete", "access_group", group.name)
    await session.commit()
    return {"id": group_id, "deleted": True}


@router.post("/workspaces/{workspace_id}/members")
async def add_members(
    workspace_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Put several people in a class at once.

    Enrolling thirty students one dropdown at a time is thirty chances to skip
    one, and the one skipped is found weeks later by a person who cannot call
    what their classmates can.

    Names already present are counted, not rejected: re-running the same list
    after a partial failure has to be safe, or nobody will dare re-run it.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Workspace not found.")
    await _assert_owns(session, actor, workspace_id)

    wanted = [str(u) for u in (payload.get("user_ids") or [])]
    if not wanted:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Send {'user_ids': [...]}.")

    known = {
        uid for (uid,) in await session.execute(
            select(User.id).where(User.id.in_(wanted))
        )
    }
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"Unknown user id(s): {', '.join(unknown[:5])}"
            + (f" and {len(unknown) - 5} more" if len(unknown) > 5 else ""),
        )

    already = {
        uid for (uid,) in await session.execute(
            select(Membership.user_id).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id.in_(known),
            )
        )
    }
    added = sorted(known - already)
    for user_id in added:
        session.add(Membership(workspace_id=workspace_id, user_id=user_id))

    await audit(session, request, actor, "workspace.members.add", "workspace",
                workspace_id, {"added": len(added), "already": len(already)})
    await session.commit()

    allowed = int((await session.execute(
        select(func.count()).select_from(WorkspaceModel).where(
            WorkspaceModel.workspace_id == workspace_id,
            WorkspaceModel.enabled.is_(True),
        )
    )).scalar() or 0)
    bundles = int((await session.execute(
        select(func.count()).select_from(WorkspaceAccessGroup).where(
            WorkspaceAccessGroup.workspace_id == workspace_id
        )
    )).scalar() or 0)
    return {
        "workspace_id": workspace_id,
        "code": workspace.code,
        "added": len(added),
        "already_in": len(already),
        # The same warning the single join gives: joining an empty class takes
        # access away rather than granting any.
        "warning": (
            "This workspace has no models enabled, so its members can call "
            "nothing. Set its models before anyone tries to use it."
            if not allowed and not bundles else ""
        ),
    }


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Remove a workspace that is genuinely finished with.

    Refused while anybody is still in it or any key is still pinned to it. A
    pinned key whose workspace has gone permits nothing, and nothing on screen
    would say why — the owner would find out by being refused. Take the members
    out and re-issue or revoke those keys first, or suspend it instead, which
    stops it granting anything and can be undone.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Workspace not found.")

    members = int((await session.execute(
        select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace_id
        )
    )).scalar() or 0)
    pinned = int((await session.execute(
        select(func.count()).select_from(ApiKey).where(
            ApiKey.workspace_id == workspace_id, ApiKey.revoked_at.is_(None)
        )
    )).scalar() or 0)
    if members or pinned:
        parts = []
        if members:
            parts.append(f"{members} member(s)")
        if pinned:
            parts.append(f"{pinned} key(s) issued for it")
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"'{workspace.code}' still has {' and '.join(parts)}. Clear those "
            "first, or suspend it instead — that stops it granting anything and "
            "can be undone.",
            details={"members": members, "pinned_keys": pinned},
        )

    await session.execute(
        delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id)
    )
    await session.execute(
        delete(WorkspaceAccessGroup).where(
            WorkspaceAccessGroup.workspace_id == workspace_id
        )
    )
    await session.execute(
        delete(QuotaPolicy).where(QuotaPolicy.workspace_id == workspace_id)
    )
    await session.delete(workspace)
    await audit(session, request, actor, "workspace.delete", "workspace", workspace.code)
    await session.commit()
    return {"id": workspace_id, "code": workspace.code, "deleted": True}


WORKSPACE_STATUSES = ("active", "suspended")


@router.patch("/workspaces/{workspace_id}/status")
async def set_workspace_status(
    workspace_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Put a class on hold, or take it off hold.

    `Workspace.status` shipped in the first release and nothing ever read it -
    the same shape of problem as membership before v1.5, a field that looks like
    a switch and is wired to nothing. A suspended workspace now grants no models.

    Different from revoking its members' keys in the way that matters: it can be
    undone. End of term, a class under investigation, a course paused between
    intakes - none of those should destroy credentials people will need again.
    """
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Workspace not found.")
    await _assert_owns(session, actor, workspace_id)

    wanted = str(payload.get("status", "")).strip()
    if wanted not in WORKSPACE_STATUSES:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"status must be one of {', '.join(WORKSPACE_STATUSES)}.",
        )

    workspace.status = wanted
    await audit(session, request, actor, "workspace.status", "workspace",
                workspace_id, {"status": wanted})
    await session.commit()

    members = await session.execute(
        select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace_id
        )
    )
    return {
        "id": workspace_id,
        "code": workspace.code,
        "status": wanted,
        "members_affected": int(members.scalar() or 0),
    }


@router.get("/workspaces")
async def list_workspaces(
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Workspace).order_by(Workspace.code)
    scope = await _scope(session, actor)
    if scope is not None:
        stmt = stmt.where(Workspace.id.in_(scope)) if scope else stmt.where(False)
    result = await session.execute(stmt)
    spaces = list(result.scalars())

    # โมเดลที่อนุญาตไว้ต้องมากับรายการนี้ ไม่งั้นหน้าเว็บวาด checkbox เป็นว่างทุกครั้ง
    # แล้วกด Save ทีเดียวรายการที่ตั้งไว้หายหมด — UI ที่โกหกสถานะปัจจุบันอันตรายกว่าไม่มี
    allowed: dict[str, list[str]] = {}
    held: dict[str, list[str]] = {}
    members: dict[str, list[dict[str, str]]] = {}
    if spaces:
        rows = await session.execute(
            select(WorkspaceModel.workspace_id, WorkspaceModel.model_alias)
            .where(WorkspaceModel.enabled.is_(True))
        )
        for workspace_id, alias in rows:
            allowed.setdefault(workspace_id, []).append(alias)
        bundles = await session.execute(
            select(WorkspaceAccessGroup.workspace_id, WorkspaceAccessGroup.access_group_id)
        )
        for workspace_id, group_id in bundles:
            held.setdefault(workspace_id, []).append(group_id)
        # Who is in each class, from the class's side. Asking "who is in CS101"
        # by reading down a list of two hundred people is not an answer.
        roster = await session.execute(
            select(Membership.workspace_id, User.id, User.external_id, User.display_name)
            .join(User, User.id == Membership.user_id)
            .order_by(User.external_id)
        )
        for workspace_id, uid, external_id, display_name in roster:
            members.setdefault(workspace_id, []).append(
                {"id": uid, "external_id": external_id, "display_name": display_name}
            )

    return {
        "data": [
            {
                "id": c.id,
                "code": c.code,
                "name": c.name,
                "term": c.term,
                "status": c.status,
                "models": sorted(allowed.get(c.id, [])),
                "access_groups": sorted(held.get(c.id, [])),
                "default_member_models": list(c.default_member_models or []),
                "default_access_groups": list(c.default_access_groups or []),
                "default_key_days": c.default_key_days or 0,
                "members": members.get(c.id, []),
            }
            for c in spaces
        ]
    }


@router.post("/workspaces/{workspace_id}/models")
async def set_workspace_models(
    workspace_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Replace the allow-list of aliases for a workspace."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Workspace not found.")
    await _assert_owns(session, actor, workspace_id)

    aliases = payload.get("models", [])
    known = set(state.registry.snapshot.models)
    unknown = [a for a in aliases if a not in known]
    if unknown:
        raise GatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Unknown model alias(es): {', '.join(unknown)}.",
            details={"known_models": sorted(known)},
        )

    # A manager cannot hand out a model they are not allowed to call themselves.
    # Without this, scoping the admin plane would be theatre: enable the model
    # for a workspace you are in, and you have granted yourself access to it.
    await _assert_may_grant(session, actor, aliases, state)

    # Bundles are set through the same call because they answer the same
    # question. Omitting the field leaves them alone, so a caller that predates
    # bundles does not wipe them by not mentioning them.
    groups: list[str] | None = None
    if "access_groups" in payload:
        groups = [str(g) for g in (payload.get("access_groups") or [])]
        found = list((await session.execute(
            select(AccessGroup).where(AccessGroup.id.in_(groups))
        )).scalars()) if groups else []
        missing = set(groups) - {g.id for g in found}
        if missing:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"Unknown access group(s): {', '.join(sorted(missing))}.",
            )
        # Handing out a bundle is handing out its models, so it is held to the
        # same rule: nobody gives away what they cannot use themselves.
        bundled = sorted({a for g in found for a in (g.models or [])})
        await _assert_may_grant(session, actor, bundled, state)

    await session.execute(delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id))
    for alias in aliases:
        session.add(WorkspaceModel(workspace_id=workspace_id, model_alias=alias, enabled=True))

    if groups is not None:
        await session.execute(
            delete(WorkspaceAccessGroup).where(
                WorkspaceAccessGroup.workspace_id == workspace_id
            )
        )
        for group_id in groups:
            session.add(
                WorkspaceAccessGroup(workspace_id=workspace_id, access_group_id=group_id)
            )

    # What a key issued to a member of this class starts as. Omitted fields are
    # left alone, so a caller that predates them does not clear them.
    if "default_member_models" in payload:
        defaults = [str(a) for a in (payload.get("default_member_models") or [])]
        await _known_aliases(state, defaults)
        # A default naming something this class cannot call produces a key whose
        # list and whose class have nothing in common, so it can call nothing at
        # all - and the owner finds out by being refused. The two settings are
        # not in competition; this is what keeps them from contradicting.
        reachable = set(aliases) | {
            a for g in await session.execute(
                select(AccessGroup.models)
                .join(WorkspaceAccessGroup,
                      WorkspaceAccessGroup.access_group_id == AccessGroup.id)
                .where(WorkspaceAccessGroup.workspace_id == workspace_id,
                       AccessGroup.enabled.is_(True))
            ) for a in (g[0] or [])
        }
        beyond = sorted(set(defaults) - reachable)
        if beyond:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"This workspace does not allow {', '.join(beyond)}, so a key "
                "starting there could call nothing. Tick them above first, or "
                "leave them out of the default.",
                details={"beyond": beyond},
            )
        workspace.default_member_models = defaults
    if "default_access_groups" in payload:
        workspace.default_access_groups = [
            str(g) for g in (payload.get("default_access_groups") or [])
        ]
    if "default_key_days" in payload:
        workspace.default_key_days = max(int(payload.get("default_key_days") or 0), 0)

    await audit(
        session,
        request,
        actor,
        "workspace.models.set",
        "workspace",
        workspace_id,
        {"models": aliases, "access_groups": groups},
    )
    await session.commit()
    return {"workspace_id": workspace_id, "models": aliases}


@router.post("/workspaces/{workspace_id}/join")
async def join(
    workspace_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user_id = payload.get("user_id")
    if not user_id or await session.get(User, user_id) is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Unknown user_id.")
    if await session.get(Workspace, workspace_id) is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Workspace not found.")
    await _assert_owns(session, actor, workspace_id)
    existing = await session.execute(
        select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    )
    if existing.scalar_one_or_none():
        return {"workspace_id": workspace_id, "user_id": user_id, "status": "already_joined"}
    session.add(
        Membership(
            workspace_id=workspace_id, user_id=user_id, role=payload.get("role", "member")
        )
    )
    await audit(session, request, actor, "workspace.join", "workspace", workspace_id, payload)
    await session.commit()

    # Membership now decides which models someone may call, so adding a person
    # to a workspace with an empty allow-list takes their access away rather
    # than granting any. Say so here, while whoever did it is still looking.
    allowed = await session.execute(
        select(func.count()).select_from(WorkspaceModel).where(
            WorkspaceModel.workspace_id == workspace_id,
            WorkspaceModel.enabled.is_(True),
        )
    )
    warning = ""
    if not int(allowed.scalar() or 0):
        warning = (
            "This workspace has no models enabled, so its members can call "
            "nothing. Set its models before anyone tries to use it."
        )
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "status": "joined",
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
class ApiKeyIn(BaseModel):
    user_id: str
    workspace_id: str | None = None
    name: str = ""
    expires_in_days: int | None = 180
    scopes: list[str] = Field(default_factory=list)
    # จำกัด key ใบนี้ให้ใช้ได้เฉพาะ alias เหล่านี้ · ว่าง = ไม่จำกัดเพิ่ม
    models: list[str] = Field(default_factory=list)
    # มัดที่ระบุบน key · รวมกับ models ข้างบนก่อน แล้วจึงไปตัดกับสิทธิ์ของเจ้าของ
    access_groups: list[str] = Field(default_factory=list)
    # person | service · ไม่เปลี่ยนกติกาสิทธิ์ มีไว้ให้แยกออกในรายการ
    kind: str = "person"


async def _workspace_defaults(
    session: AsyncSession, payload: ApiKeyIn
) -> Workspace | None:
    """The class whose defaults should fill in what the caller left blank.

    The workspace named on the key, if there is one. Otherwise the owner's, but
    only when they are in exactly one - with two classes there is no right
    answer, and guessing would hand somebody the wrong term's settings.
    """
    if payload.workspace_id:
        return await session.get(Workspace, payload.workspace_id)
    rows = await session.execute(
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == payload.user_id)
    )
    spaces = list(rows.scalars())
    return spaces[0] if len(spaces) == 1 else None


@router.post("/api-keys", status_code=201)
async def create_api_key(
    payload: ApiKeyIn,
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """The plaintext key is returned exactly once and never stored."""
    user = await session.get(User, payload.user_id)
    if user is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Unknown user_id.")
    if user.role == "admin" and not actor.is_admin:
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE, "Only an admin can issue an admin key."
        )

    # Issuing a key is the act of handing out access, so it is where the scope
    # matters most: to somebody in your workspaces, for a workspace of yours,
    # naming models you can use yourself.
    visible = await _visible_users(session, actor)
    if visible is not None and payload.user_id not in visible:
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            "You can only issue keys to people in your own workspaces.",
        )
    if payload.workspace_id:
        await _assert_owns(session, actor, payload.workspace_id)

    # alias ที่ไม่มีอยู่จริงบน key = key ที่เรียกอะไรไม่ได้เลย และไม่มีอะไรบอกจนกว่า
    # ผู้ใช้จะลอง · ตรวจตอนออกดีกว่าให้ไปเจอตอนใช้
    if payload.models:
        known = set(state.registry.snapshot.models)
        unknown = [a for a in payload.models if a not in known]
        if unknown:
            raise GatewayError(
                ErrorCode.MODEL_NOT_FOUND,
                f"Unknown model alias(es): {', '.join(unknown)}.",
                details={"known_models": sorted(known)},
            )
    await _assert_may_grant(session, actor, payload.models, state)

    if payload.access_groups:
        found = list((await session.execute(
            select(AccessGroup).where(AccessGroup.id.in_(payload.access_groups))
        )).scalars())
        missing = set(payload.access_groups) - {g.id for g in found}
        if missing:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"Unknown access group(s): {', '.join(sorted(missing))}.",
            )
        await _assert_may_grant(
            session, actor, sorted({a for g in found for a in (g.models or [])}), state
        )

    # Fill in what the caller left blank from the class's defaults. Only blanks:
    # an explicit empty list is a decision ("this key is unrestricted") and
    # overwriting it would be the console arguing with the person using it.
    #
    # "The caller did not choose" is `model_fields_set`, never the value: every
    # one of these fields has a default of its own, and reading the default as
    # a choice is what made the workspace's expiry silently lose to the schema's
    # 180 days.
    applied: dict[str, Any] = {}
    blanks = {"models", "access_groups", "expires_in_days"} - payload.model_fields_set
    if blanks:
        home = await _workspace_defaults(session, payload)
        if home is not None:
            if "models" in blanks and home.default_member_models:
                payload.models = list(home.default_member_models)
                applied["models"] = payload.models
            if "access_groups" in blanks and home.default_access_groups:
                payload.access_groups = list(home.default_access_groups)
                applied["access_groups"] = payload.access_groups
            if "expires_in_days" in blanks and home.default_key_days:
                payload.expires_in_days = home.default_key_days
                applied["expires_in_days"] = payload.expires_in_days
            if applied:
                applied["from_workspace"] = home.code

    plaintext, prefix, digest = generate_api_key()
    expires_at = (
        utcnow() + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    api_key = ApiKey(
        user_id=payload.user_id,
        workspace_id=payload.workspace_id,
        name=payload.name,
        key_prefix=prefix,
        key_hash=digest,
        scopes=payload.scopes,
        models=payload.models,
        access_groups=payload.access_groups,
        kind="service" if payload.kind == "service" else "person",
        expires_at=expires_at,
    )
    session.add(api_key)
    await audit(session, request, actor, "apikey.create", "user", payload.user_id)
    await session.commit()
    return {
        "id": api_key.id,
        "api_key": plaintext,
        "key_prefix": prefix,
        "user_id": payload.user_id,
        "workspace_id": payload.workspace_id,
        "models": payload.models,
        "access_groups": payload.access_groups,
        "kind": api_key.kind,
        "expires_at": expires_at.isoformat() if expires_at else None,
        # What was filled in for you, and where it came from. A default that
        # applies silently is a setting nobody knows they have.
        "applied_defaults": applied,
        "warning": "Store this key now. It cannot be retrieved again.",
    }


@router.delete("/workspaces/{workspace_id}/members/{user_id}")
async def leave(
    workspace_id: str,
    user_id: str,
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """เอาคนออกจาก workspace

    `join` มีมาตั้งแต่ต้น แต่ไม่มีทางออก — ใส่ผิดคนแล้วแก้ไม่ได้เลยนอกจากแก้ฐานข้อมูล
    เอง และคนที่จบเทอมไปแล้วก็ยังค้างอยู่ในรายชื่อตลอดไป

    key ที่ผูกกับ workspace นี้ไม่ถูกแตะ — มันหยุดใช้ quota ของ workspace เองเมื่อ
    สิทธิ์หายไป การไปเพิกถอน key ให้ด้วยเป็นการตัดสินใจแทนผู้ใช้ในเรื่องที่กู้คืนไม่ได้
    """
    await _assert_owns(session, actor, workspace_id)
    result = await session.execute(
        select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "That person is not in this workspace.")
    await session.delete(membership)
    await audit(session, request, actor, "workspace.leave", "workspace",
                workspace_id, {"user_id": user_id})
    await session.commit()
    return {"workspace_id": workspace_id, "user_id": user_id, "status": "removed"}


@router.get("/api-keys")
async def list_api_keys(
    user_id: str | None = None,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc()).limit(500)
    if user_id:
        stmt = stmt.where(ApiKey.user_id == user_id)
    visible = await _visible_users(session, actor)
    if visible is not None:
        stmt = stmt.where(ApiKey.user_id.in_(visible))
    result = await session.execute(stmt)
    return {
        "data": [
            {
                "id": k.id,
                "user_id": k.user_id,
                "workspace_id": k.workspace_id,
                "name": k.name,
                "key_prefix": k.key_prefix,
                "models": list(k.models or []),
                "access_groups": list(k.access_groups or []),
                "kind": k.kind or "person",
                "revoked": k.revoked_at is not None,
                "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            }
            for k in result.scalars()
        ]
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    request: Request,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "API key not found.")
    visible = await _visible_users(session, actor)
    if visible is not None and api_key.user_id not in visible:
        # Same message as "not found": whether a key exists is not something a
        # manager outside its workspace should be able to probe for.
        raise GatewayError(ErrorCode.INVALID_REQUEST, "API key not found.")
    api_key.revoked_at = utcnow()
    await audit(session, request, actor, "apikey.revoke", "apikey", key_id)
    await session.commit()
    return {"id": key_id, "revoked": True}


@router.delete("/api-keys/{key_id}/purge")
async def purge_api_key(
    key_id: str,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Delete a revoked key's row for good.

    Revoking is what stops a key working; the row stays so the listing can show
    what was withdrawn and when. After a term of issuing and rotating, that list
    is mostly tombstones and the live keys are hard to find in it.

    Only a key that is already revoked can be purged. Deleting a live key would
    lock somebody out with nothing to point at afterwards, so revoking stays a
    separate, deliberate step.

    Usage rows survive: `usage.api_key_id` is a plain column, not a foreign key,
    exactly so history outlives the key. What the row carried and nothing else
    does - the name and prefix - goes into the audit entry before it is gone.
    """
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "API key not found.")
    if api_key.revoked_at is None:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "Revoke this key before deleting it. Deleting a live key would cut "
            "off whoever is holding it with no record of which key it was.",
        )

    detail = f"{api_key.name or '(unnamed)'} {api_key.key_prefix}"
    await audit(session, request, actor, "apikey.purge", "apikey", f"{key_id} {detail}")
    await session.delete(api_key)
    await session.commit()
    return {"id": key_id, "purged": True}


@router.post("/api-keys/purge-revoked")
async def purge_revoked_api_keys(
    request: Request,
    older_than_days: int = 0,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Clear out revoked keys in one go.

    `older_than_days` keeps recent revocations visible - useful right after a
    rotation, when seeing what was just withdrawn is the point. 0 removes all
    of them.
    """
    stmt = select(ApiKey).where(ApiKey.revoked_at.is_not(None))
    if older_than_days > 0:
        cutoff = utcnow() - timedelta(days=older_than_days)
        stmt = stmt.where(ApiKey.revoked_at < cutoff)
    keys = list((await session.execute(stmt)).scalars())

    for api_key in keys:
        await session.delete(api_key)
    # One audit line for the sweep - a line per key would bury the log with the
    # thing being cleaned up.
    await audit(session, request, actor, "apikey.purge_revoked", "apikey",
                f"{len(keys)} key(s), older_than_days={older_than_days}")
    await session.commit()
    return {"purged": len(keys)}


# ---------------------------------------------------------------------------
# Quota policies
# ---------------------------------------------------------------------------
class QuotaPolicyIn(BaseModel):
    # ชื่อที่คนอ่านออกว่านโยบายนี้เขียนไว้เพื่ออะไร · scope+เป้าหมายบอกโค้ดได้ แต่ไม่ได้
    # บอกคนที่กำลังมองอยู่หกใบว่าใบไหนคือใบที่เขียนไว้ตอนสอบ
    name: str = ""
    scope: str = "global"
    workspace_id: str | None = None
    user_id: str | None = None
    model_alias: str | None = None
    # หรือทั้งมัด · มัดคือชุดโมเดลที่มีชื่ออยู่แล้ว จึงเล็งไปที่มัดแทนที่จะสร้างรายชื่อ
    # โมเดลชุดที่สองซึ่งต้องคอยไล่ให้ตรงกัน
    access_group_id: str | None = None
    window: str = "day"
    max_requests: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_images: int = 0
    # ลิมิตต่อนาที นับต่อคน · 0 = ไม่จำกัด ซึ่งเป็นค่าตั้งต้น
    max_requests_per_minute: int = 0
    max_tokens_per_minute: int = 0
    # ให้สิทธิ์ชั่วคราว · ครบกำหนดแล้วนโยบายเลิกมีผลเอง ไม่ต้องจำไปลบ
    expires_in_days: int | None = None


@router.post("/quota-policies", status_code=201)
async def create_quota_policy(
    payload: QuotaPolicyIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if payload.window not in {"day", "month", "term"}:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "window must be day, month or term.")
    if payload.model_alias and payload.access_group_id:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "Name one model or one bundle, not both — two answers to the same "
            "question is a policy nobody can predict.",
        )
    if payload.access_group_id and not await session.get(
        AccessGroup, payload.access_group_id
    ):
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Access group not found.")
    fields = payload.model_dump()
    days = fields.pop("expires_in_days", None)
    policy = QuotaPolicy(
        **fields,
        expires_at=utcnow() + timedelta(days=days) if days else None,
    )
    session.add(policy)
    await audit(session, request, actor, "quota.create", "quota", payload.scope)
    await session.commit()
    return {
        "id": policy.id,
        **fields,
        "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
    }


@router.get("/quota-policies")
async def list_quota_policies(
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(QuotaPolicy).where(QuotaPolicy.enabled.is_(True))
    scope = await _scope(session, actor)
    if scope is not None:
        # The global policy applies to this manager too, so it stays visible.
        # A policy aimed at another workspace does not concern them.
        stmt = stmt.where(
            (QuotaPolicy.workspace_id.is_(None) & QuotaPolicy.user_id.is_(None))
            | QuotaPolicy.workspace_id.in_(scope)
            | (QuotaPolicy.user_id == actor.user_id)
        )
    result = await session.execute(stmt)
    return {
        "data": [
            {
                "id": p.id,
                "name": p.name,
                "scope": p.scope,
                "access_group_id": p.access_group_id,
                "workspace_id": p.workspace_id,
                "user_id": p.user_id,
                "model_alias": p.model_alias,
                "window": p.window,
                "max_requests": p.max_requests,
                "max_input_tokens": p.max_input_tokens,
                "max_output_tokens": p.max_output_tokens,
                "max_images": p.max_images,
                "max_requests_per_minute": p.max_requests_per_minute,
                "max_tokens_per_minute": p.max_tokens_per_minute,
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            }
            for p in result.scalars()
        ]
    }


@router.delete("/quota-policies/{policy_id}")
async def delete_quota_policy(
    policy_id: str,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Remove a quota policy.

    Policies were only ever created, so a wrong one could be superseded but not
    taken away - and since the most specific match wins, a stale narrow policy
    quietly keeps beating the broader one meant to replace it.

    Deleting shifts everyone it covered to the next policy out (user -> workspace
    -> global), which is a real change in what members can spend. The audit entry
    records what the policy was, since the row will not be there to look at.
    """
    policy = await session.get(QuotaPolicy, policy_id)
    if policy is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Quota policy not found.")

    detail = (f"scope={policy.scope} window={policy.window} "
              f"requests={policy.max_requests} user={policy.user_id or '-'} "
              f"workspace={policy.workspace_id or '-'} model={policy.model_alias or '-'}")
    await audit(session, request, actor, "quota.delete", "quota", f"{policy_id} {detail}")
    await session.delete(policy)
    await session.commit()
    return {"id": policy_id, "deleted": True}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@router.get("/models")
async def admin_models(
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Full registry view including upstream names and endpoints (PRD §6)."""
    snapshot = state.registry.snapshot
    health = state.router.health_report()
    data = []
    for alias, model in snapshot.models.items():
        data.append(
            {
                "alias": alias,
                "display_name": model.metadata.display_name,
                # Returned so the console's Edit form can round-trip a model
                # without silently dropping fields it did not show.
                "description": model.metadata.description,
                "tags": model.metadata.tags,
                "visibility": model.metadata.visibility.value,
                "upstream_model": model.spec.upstream_model,
                "purpose": [p.value for p in model.spec.purpose],
                "capabilities": model.spec.capabilities.model_dump(),
                "protocols": model.spec.protocols.model_dump(),
                "modalities": {
                    "input": [m.value for m in model.spec.modalities.input],
                    "output": [m.value for m in model.spec.modalities.output],
                },
                "limits": model.spec.limits.model_dump(),
                "agent_clients": {
                    k: v.model_dump() for k, v in model.spec.agent_clients.items()
                },
                "badges": compatibility_badges(model),
                "enabled": model.spec.enabled,
                "endpoints": [
                    {
                        "name": e.name,
                        "server_type": e.server_type.value,
                        "base_url": e.normalized_base_url,
                        "upstream_model": e.upstream_model,
                        "api_key_env": e.api_key_env,
                        "priority": e.priority,
                        "weight": e.weight,
                        "max_concurrency": e.max_concurrency,
                        "health_path": e.health_path,
                        "protocols": e.protocols.model_dump(),
                        "modalities": e.modalities.model_dump(),
                        "enabled": e.enabled,
                        "health": health.get(f"{alias}:{e.name}", {}),
                    }
                    for e in model.spec.endpoints
                ],
            }
        )
    return {"data": data, "errors": snapshot.errors}


@router.post("/registry/reload")
async def reload_registry(
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    snapshot = state.registry.reload()
    state.quota.update_defaults(snapshot.gateway.quota_defaults)
    await sync_model_projection(session, state)
    await audit(session, request, actor, "registry.reload")
    await session.commit()
    return {
        "models": sorted(snapshot.models),
        "errors": snapshot.errors,
        "status": "reloaded",
    }


async def sync_model_projection(session: AsyncSession, state: AppState) -> None:
    """Mirror the YAML registry into `models` so usage/compat rows have FKs."""
    snapshot = state.registry.snapshot
    result = await session.execute(select(ModelRecord))
    existing = {record.alias: record for record in result.scalars()}

    for alias, model in snapshot.models.items():
        caps = model.spec.capabilities
        record = existing.get(alias) or ModelRecord(alias=alias)
        record.display_name = model.metadata.display_name
        record.upstream_model = model.spec.upstream_model
        record.purpose = [p.value for p in model.spec.purpose]
        record.context_length = model.spec.limits.context_tokens
        record.max_output_tokens = model.spec.limits.max_output_tokens
        record.supports_text = True
        record.supports_image = caps.vision
        record.supports_audio = caps.audio
        record.supports_video = False
        record.supports_streaming = caps.streaming
        record.supports_tools = caps.tools
        record.supports_reasoning = caps.reasoning
        record.supports_agentic = caps.agentic
        record.supports_openai = model.spec.protocols.openai
        record.supports_anthropic = model.spec.protocols.anthropic
        claude_code = model.spec.agent_clients.get("claude_code")
        record.claude_code_compatible = bool(claude_code and claude_code.enabled)
        record.visibility = model.metadata.visibility.value
        record.enabled = model.spec.enabled
        if alias not in existing:
            session.add(record)

    for alias, record in existing.items():
        if alias not in snapshot.models:
            record.enabled = False


# ---------------------------------------------------------------------------
# Compatibility results
# ---------------------------------------------------------------------------
class CompatibilityIn(BaseModel):
    feature: str
    status: str  # pass | fail | degraded | not_tested
    test_version: str = "1.0"
    latency_ms: int | None = None
    notes: str = ""


@router.post("/models/{alias}/compatibility")
async def record_compatibility(
    alias: str,
    payload: CompatibilityIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    if alias not in state.registry.snapshot.models:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"Unknown model '{alias}'.")
    if payload.status not in {"pass", "fail", "degraded", "not_tested"}:
        raise GatewayError(ErrorCode.INVALID_REQUEST, f"Bad status '{payload.status}'.")

    await sync_model_projection(session, state)
    await session.flush()
    result = await session.execute(select(ModelRecord).where(ModelRecord.alias == alias))
    record = result.scalar_one()

    existing = await session.execute(
        select(ModelCompatibility).where(
            ModelCompatibility.model_id == record.id,
            ModelCompatibility.feature == payload.feature,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = ModelCompatibility(model_id=record.id, feature=payload.feature)
        session.add(row)
    row.status = payload.status
    row.tested_at = utcnow()
    row.test_version = payload.test_version
    row.latency_ms = payload.latency_ms
    row.notes = payload.notes

    await audit(
        session, request, actor, "model.compatibility", "model", alias, payload.model_dump()
    )
    await session.commit()
    return {"model": alias, **payload.model_dump(), "tested_at": row.tested_at.isoformat()}


@router.get("/models/{alias}/compatibility")
async def get_compatibility(
    alias: str,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    await _assert_may_read_model(session, actor, alias, state)
    result = await session.execute(select(ModelRecord).where(ModelRecord.alias == alias))
    record = result.scalar_one_or_none()
    if record is None:
        return {"model": alias, "results": [], "status": "NOT TESTED"}
    rows = await session.execute(
        select(ModelCompatibility).where(ModelCompatibility.model_id == record.id)
    )
    results = [
        {
            "feature": r.feature,
            "status": r.status,
            "tested_at": r.tested_at.isoformat() if r.tested_at else None,
            "latency_ms": r.latency_ms,
            "notes": r.notes,
        }
        for r in rows.scalars()
    ]
    # READY requires the two features every client depends on.
    required = {"chat", "streaming"}
    passed = {r["feature"] for r in results if r["status"] == "pass"}
    status = "READY" if required.issubset(passed) else "NOT READY"
    if any(r["status"] == "fail" for r in results):
        status = "DEGRADED"
    return {"model": alias, "results": results, "status": status}


# ---------------------------------------------------------------------------
# Usage reporting
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Registry authoring (console)
# ---------------------------------------------------------------------------
class ModelDefinitionIn(BaseModel):
    """A full model document as the wizard builds it."""

    model_config = {"extra": "allow"}

    metadata: dict[str, Any]
    spec: dict[str, Any]
    apiVersion: str = "litegate.dev/v1"
    kind: str = "Model"


@router.get("/registry/status")
async def registry_status(
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Tells the console whether 'save to disk' can be offered at all."""
    config_dir = state.settings.config_dir
    return {
        "config_dir": str(config_dir),
        "writable": registry_writable(config_dir),
        "reload_seconds": state.settings.registry_reload_seconds,
        "errors": state.registry.snapshot.errors,
    }


# ---------------------------------------------------------------------------
# Console assistant
# ---------------------------------------------------------------------------
class AssistantModelIn(BaseModel):
    # Empty means "choose automatically", which is a valid answer and the
    # default. It is not the same as "no assistant".
    alias: str = Field(default="", max_length=64)


@router.get("/assistant")
async def assistant_settings(
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """What the assistant runs on, and how well every candidate would suit it.

    Candidates are ranked, not filtered: "why can I not pick that one?" is a
    question an operator actually asks, and a model missing from the list
    answers it with silence. Unusable ones come with the blocker that made them
    unusable.
    """
    snapshot = state.registry.snapshot
    candidates = [m for m in snapshot.models.values() if m.spec.capabilities.chat]

    health: dict[str, bool] = {}
    for entry in state.router.health_report().values():
        alias = entry["model"]
        # A model with several endpoints is healthy if any endpoint is - that
        # is what routing will do with the next request.
        health[alias] = health.get(alias, False) or bool(entry["healthy"])

    compatibility = await _compatibility_by_alias(session)
    ranked = assistant_fit.rank(candidates, health=health, compatibility=compatibility)

    row = await session.get(GatewaySetting, ASSISTANT_MODEL_KEY)
    pinned = row.value if row is not None else ""
    if row is not None and row.value:
        source = "console"
    elif row is not None:
        # An administrator cleared the pin, which is a decision: it means
        # "choose automatically" and it deliberately overrides the deploy-time
        # variable. Reporting "console" here would be true but useless - the
        # question the field answers is *what is choosing the model*.
        source = "automatic"
    elif state.settings.assistant_model:
        # Set at deploy time via GW_ASSISTANT_MODEL. Shown as such so nobody
        # hunts for a console setting that was never made here.
        source, pinned = "environment", state.settings.assistant_model
    else:
        source = "automatic"

    automatic = next((fit.alias for fit in ranked if fit.usable), None)
    return {
        "pinned": pinned,
        "source": source,
        "effective": pinned or automatic,
        "automatic_choice": automatic,
        "candidates": [fit.to_dict() for fit in ranked],
    }


@router.put("/assistant")
async def set_assistant_model(
    payload: AssistantModelIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Pin the assistant to an alias, or clear the pin to choose automatically.

    Refuses an alias that cannot serve the role, and says which check it failed.
    Accepting it would produce a chat box that is visibly broken with no
    explanation, and the person who set it would be the last to find out.
    """
    alias = payload.alias.strip()
    if alias:
        model = state.registry.snapshot.models.get(alias)
        if model is None:
            raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"No model with alias '{alias}'.")
        compatibility = await _compatibility_by_alias(session)
        fit = assistant_fit.assess(model, compatibility=compatibility.get(alias))
        if not fit.usable:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"'{alias}' cannot serve as the assistant: "
                + " ".join(reason.detail for reason in fit.blockers),
            )

    row = await session.get(GatewaySetting, ASSISTANT_MODEL_KEY)
    if row is None:
        row = GatewaySetting(key=ASSISTANT_MODEL_KEY)
        session.add(row)
    row.value = alias
    row.updated_at = utcnow()
    row.updated_by = actor.user_id
    await audit(
        session, request, actor, "assistant.model", "setting", ASSISTANT_MODEL_KEY,
        {"alias": alias or "(automatic)"},
    )
    await session.commit()
    return await assistant_settings(actor=actor, state=state, session=session)


async def _compatibility_by_alias(session: AsyncSession) -> dict[str, dict[str, str]]:
    """Test-suite verdicts, keyed by alias then feature.

    One query rather than one per model: the console asks for this every time
    the assistant tab opens, and a fleet of thirty models would otherwise mean
    thirty round trips to render one list.
    """
    rows = (
        await session.execute(
            select(ModelRecord.alias, ModelCompatibility.feature, ModelCompatibility.status)
            .join(ModelCompatibility, ModelCompatibility.model_id == ModelRecord.id)
        )
    ).all()
    by_alias: dict[str, dict[str, str]] = {}
    for alias, feature, status in rows:
        by_alias.setdefault(alias, {})[feature] = status
    return by_alias


# ---------------------------------------------------------------------------
# Deploy-tool integration
# ---------------------------------------------------------------------------
class LmdsConnectionIn(BaseModel):
    base_url: str = Field(default="", max_length=200)
    # Write-only. Absent means "keep what is stored"; empty string clears it.
    token: str | None = Field(default=None, max_length=200)


async def _lmds_connection(session: AsyncSession) -> lmds.Connection:
    base = await session.get(GatewaySetting, lmds.BASE_URL_KEY)
    token = await session.get(GatewaySetting, TOKEN_KEY_SETTING)
    return lmds.Connection(
        base_url=base.value if base else "", token=token.value if token else ""
    )


@router.get("/integrations/lmds")
async def lmds_settings(
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    connection = await _lmds_connection(session)
    return {
        "base_url": connection.base_url,
        "configured": connection.configured,
        # Never returned, not even to an admin: a console that can display a
        # token is a console that leaks it into a screenshot.
        "has_token": bool(connection.token),
        "appliable_issues": sorted(lmds.APPLIABLE),
    }


@router.put("/integrations/lmds")
async def set_lmds_settings(
    payload: LmdsConnectionIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Connect a deploy tool, or clear the connection with an empty base_url."""
    base_url = payload.base_url.strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "base_url must start with http:// or https://"
        )

    await _store(session, lmds.BASE_URL_KEY, base_url, actor)
    if payload.token is not None:
        await _store(session, TOKEN_KEY_SETTING, payload.token.strip(), actor)
    await audit(
        session, request, actor, "integration.lmds", "setting", lmds.BASE_URL_KEY,
        {"base_url": base_url or "(cleared)"},
    )
    await session.commit()
    return await lmds_settings(actor=actor, session=session)


@router.post("/integrations/lmds/test")
async def test_lmds_connection(
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Prove the connection reaches the fleet you think it does.

    A saved URL and a working connection look the same in a settings form. This
    makes an authenticated call and reports which machine answered, so nobody
    discovers they configured the staging LMDS by restarting a production model.
    """
    return await lmds.check(await _lmds_connection(session))


class ApplyFixIn(BaseModel):
    issue: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(default="", max_length=64)
    # The console sends back the parser the advice suggested, so what gets
    # applied is what the operator read - not something re-derived here that
    # might differ from what the screen said.
    parser: str = Field(default="", max_length=64)


@router.post("/models/{alias}/apply-fix")
async def apply_fix(
    alias: str,
    payload: ApplyFixIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Send one finding to the deploy tool that can fix it.

    Deliberately not "apply everything": each call names one finding, on one
    endpoint, and the console re-probes afterwards. Whether it worked is a
    question only the probe can answer, and answering it any other way would be
    reporting our own intent back as a result.
    """
    model = state.registry.snapshot.models.get(alias)
    if model is None:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"No model with alias '{alias}'.")

    endpoints = model.spec.endpoints
    if payload.endpoint:
        endpoint = next((e for e in endpoints if e.name == payload.endpoint), None)
        if endpoint is None:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"'{alias}' has no endpoint named '{payload.endpoint}'.",
            )
    elif len(endpoints) == 1:
        endpoint = endpoints[0]
    else:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"'{alias}' has {len(endpoints)} endpoints - say which one to fix.",
        )

    parser = payload.parser.strip()
    if not parser:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "No parser given - nothing to apply."
        )
    if not _PARSER_NAME.fullmatch(parser):
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "A parser name is letters, digits, underscore and hyphen only.",
        )

    if endpoint.managed_by is None:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"Endpoint '{endpoint.name}' does not record which tool deployed it, "
            "so there is nothing to send the fix to. Run the command yourself, or "
            "add `managed_by` to the model file.",
        )

    connection = await _lmds_connection(session)
    result = await lmds.apply_fix(connection, endpoint.managed_by, payload.issue, parser)

    await audit(
        session, request, actor, "model.apply_fix", "model", alias,
        {"issue": payload.issue, "endpoint": endpoint.name, **result["applied"]},
    )
    await session.commit()
    return {
        "alias": alias,
        "endpoint": endpoint.name,
        "issue": payload.issue,
        **result,
        "next": f"Re-run verification on '{alias}' to confirm the finding is gone.",
    }


async def _store(session: AsyncSession, key: str, value: str, actor: Principal) -> None:
    row = await session.get(GatewaySetting, key)
    if row is None:
        row = GatewaySetting(key=key)
        session.add(row)
    row.value = value
    row.updated_at = utcnow()
    row.updated_by = actor.user_id


@router.post("/models/preview")
async def preview_model(
    payload: ModelDefinitionIn,
    actor: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Validate a draft and render its YAML without touching disk (mode A)."""
    definition = validate_definition(payload.model_dump())
    return {
        "alias": definition.alias,
        "filename": f"{definition.alias}.yaml",
        "yaml": render_yaml(definition),
    }


@router.post("/models", status_code=201)
async def save_model(
    payload: ModelDefinitionIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Validate and write the definition into the registry (mode B)."""
    definition = validate_definition(payload.model_dump())
    existing = definition.alias in state.registry.snapshot.models

    path = write_model(state.settings.config_dir, definition)
    snapshot = state.registry.reload()
    await sync_model_projection(session, state)
    await audit(
        session,
        request,
        actor,
        "model.update" if existing else "model.create",
        "model",
        definition.alias,
        {"upstream_model": definition.spec.upstream_model, "path": str(path)},
    )
    await session.commit()

    return {
        "alias": definition.alias,
        "path": str(path),
        "created": not existing,
        "registry_errors": snapshot.errors,
        # Only this worker reloaded synchronously; the watcher covers the rest.
        "propagation_seconds": state.settings.registry_reload_seconds,
    }


@router.patch("/models/{alias}/enabled")
async def set_model_enabled(
    alias: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Turn an alias off without deleting its file, or a single endpoint off.

    `enabled` has always been honoured — the registry hides a disabled model
    from the catalogue and routing skips a disabled endpoint — but the console
    offered no way to set it. Taking a model out of service meant deleting the
    file and rebuilding it afterwards, which loses every setting that was tuned
    on the way in.

    Send `{"enabled": false}` for the alias, or add `"endpoint": "<name>"` to
    take one backend out while the others keep serving — the case when one
    machine is being worked on and the alias should stay up.
    """
    definition = state.registry.snapshot.models.get(alias)
    if definition is None:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"Unknown model '{alias}'.")
    if "enabled" not in payload:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Send {'enabled': true|false}.")

    wanted = bool(payload["enabled"])
    endpoint_name = payload.get("endpoint")
    updated = definition.model_copy(deep=True)

    if endpoint_name:
        target = next((e for e in updated.spec.endpoints if e.name == endpoint_name), None)
        if target is None:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"'{alias}' has no endpoint named '{endpoint_name}'.",
            )
        target.enabled = wanted
    else:
        updated.spec.enabled = wanted

    # "อย่างน้อยหนึ่ง endpoint ต้องเปิด" เป็นกติกาของ schema อยู่แล้ว — ตรวจซ้ำผ่าน
    # ทางเดิมแทนที่จะเขียนเงื่อนไขของตัวเอง ไม่งั้นการตั้งค่าที่ loader จะปฏิเสธ
    # จะถูกเขียนลงไฟล์ แล้วค่อยไปพังตอน reload
    validate_definition(updated.model_dump(mode="json"))

    # ปิดสวิตช์ไม่ควรทำให้คอมเมนต์ในไฟล์หาย — แก้เฉพาะบรรทัดนั้น ถ้าหาไม่เจอ
    # ค่อยถอยไปเขียนใหม่ทั้งไฟล์แบบเดิม
    path = model_path(state.settings.config_dir, alias)
    if not set_enabled_in_file(path, wanted, endpoint_name):
        path = write_model(state.settings.config_dir, updated)
    snapshot = state.registry.reload()
    await sync_model_projection(session, state)
    await audit(
        session, request, actor,
        "model.enabled" if not endpoint_name else "model.endpoint.enabled",
        "model", alias, {"enabled": wanted, "endpoint": endpoint_name},
    )
    await session.commit()
    return {
        "alias": alias,
        "endpoint": endpoint_name,
        "enabled": wanted,
        "path": str(path),
        "registry_errors": snapshot.errors,
    }


# The two knobs that decide how traffic is shared between the machines behind
# one alias. Editable on their own because they are what an operator reaches for
# when a backend is coping badly, and the full editor rewrites the whole file.
TUNABLE = {"priority": (0, 1000), "weight": (1, 100), "max_concurrency": (1, 4096)}


@router.patch("/models/{alias}/endpoints/{endpoint_name}")
async def tune_endpoint(
    alias: str,
    endpoint_name: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Change how much work one backend takes, without touching the rest.

    `priority` picks the tier: the highest tier that still has room takes
    everything, and the ones below it are standby. Give two machines the *same*
    priority and they share the work — the router sends each request to whichever
    is carrying less, so one being busy no longer makes the next person wait.

    `max_concurrency` is what makes a lower tier useful even while the top one is
    healthy: once the top tier is full, requests spill down instead of queueing.
    """
    definition = state.registry.snapshot.models.get(alias)
    if definition is None:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"Unknown model '{alias}'.")

    updated = definition.model_copy(deep=True)
    target = next((e for e in updated.spec.endpoints if e.name == endpoint_name), None)
    if target is None:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, f"'{alias}' has no endpoint named '{endpoint_name}'."
        )

    changes: dict[str, int] = {}
    for key, (low, high) in TUNABLE.items():
        if key not in payload:
            continue
        try:
            value = int(payload[key])
        except (TypeError, ValueError):
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, f"{key} must be a whole number."
            ) from None
        if not low <= value <= high:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, f"{key} must be between {low} and {high}."
            )
        setattr(target, key, value)
        changes[key] = value

    if not changes:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"Send at least one of: {', '.join(sorted(TUNABLE))}.",
        )

    validate_definition(updated.model_dump(mode="json"))

    path = model_path(state.settings.config_dir, alias)
    for key, value in changes.items():
        if not set_field_in_file(path, key, value, endpoint_name):
            path = write_model(state.settings.config_dir, updated)
            break
    snapshot = state.registry.reload()
    await audit(
        session, request, actor, "model.endpoint.tune", "model", alias,
        {"endpoint": endpoint_name, **changes},
    )
    await session.commit()
    return {
        "alias": alias,
        "endpoint": endpoint_name,
        "changed": changes,
        "path": str(path),
        "registry_errors": snapshot.errors,
    }


@router.delete("/models/{alias}")
async def delete_model_definition(
    alias: str,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    removed = delete_model(state.settings.config_dir, alias)
    if not removed:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"No registry file for '{alias}'.")
    state.registry.reload()
    await sync_model_projection(session, state)
    await audit(session, request, actor, "model.delete", "model", alias)
    await session.commit()
    return {"alias": alias, "deleted": True}


class ProbeIn(BaseModel):
    base_url: str
    upstream_model: str = ""
    api_key_env: str = ""


@router.post("/models/detect")
async def detect_capabilities(
    payload: ProbeIn,
    actor: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Probe a backend and suggest capabilities (PRD FR-39).

    The result is advisory. The admin confirms before anything is saved - the
    gateway never flips a declared capability from a probe on its own.
    """
    api_key = os.environ.get(payload.api_key_env, "") if payload.api_key_env else ""
    result = await probe_backend(payload.base_url, payload.upstream_model, api_key)
    return {"suggestion": result.to_dict(), "confirmed": False}


def _with_apply(finding: dict[str, Any], probe, endpoint) -> dict[str, Any]:
    """Attach what the console needs to offer "apply this" instead of "paste this".

    The parser travels with the finding rather than being re-derived when the
    button is pressed: what gets applied should be what the operator read, and
    a probe run in between could suggest something else.

    `confident` is carried through honestly. A wrong parser does not error - it
    quietly fails to parse anything - so a guess presented as a certainty is a
    fix that looks applied and is not.
    """
    issue = finding.get("issue")
    option = lmds.APPLIABLE.get(issue)
    if option is None:
        return finding

    served = probe.upstream_model or (probe.served_models[0] if probe.served_models else "")
    parser, confident = (
        suggest_tool_parser(served)
        if option == "tool_parser"
        else lmds.suggest_reasoning_parser(served)
    )
    managed = endpoint.managed_by
    # The reasoning advice is written with a `<parser>` placeholder because
    # build_advice() has no served-model name to work from. Here we do, so the
    # printed command matches what the button would send - two different answers
    # on one screen is how people end up applying the wrong one.
    command = finding.get("command", "").replace("<parser>", parser)
    return {
        **finding,
        "command": command,
        "parser": parser,
        "parser_confident": confident,
        # Whether *this endpoint* can be fixed remotely. A gateway can have one
        # LMDS-managed backend and three that nobody manages.
        "appliable": bool(
            managed and managed.tool == "lmds" and managed.lmds_node and managed.lmds_slug
        ),
    }


@router.get("/models/{alias}/advice")
async def model_advice(
    alias: str,
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Re-probe every backend of a registered model and report what to fix.

    This is the verification LMDS has no way to run: LMDS decides what to
    generate, the gateway measures what the running server actually does. It
    works with LMDS absent - the advice names the flags either way - and the
    findings are machine-readable so a deploy tool could consume them.
    """
    model = state.registry.snapshot.models.get(alias)
    if model is None:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"Unknown model '{alias}'.")

    backends: list[dict[str, Any]] = []
    for endpoint in model.spec.endpoints:
        if not endpoint.enabled:
            continue
        api_key = os.environ.get(endpoint.api_key_env, "") if endpoint.api_key_env else ""
        probe = await probe_backend(
            endpoint.normalized_base_url,
            upstream_model_for(model, endpoint),
            api_key,
        )
        # What the registry claims versus what the backend just did. A mismatch
        # here is the thing that silently breaks members.
        declared = model.spec.capabilities.model_dump()
        drift = [
            {
                "capability": name,
                "declared": bool(declared.get(name)),
                "measured": bool(probe.capabilities.get(name)),
            }
            for name in ("chat", "streaming", "tools", "vision")
            if name in probe.capabilities
            and bool(declared.get(name)) != bool(probe.capabilities[name])
        ]
        # The probe does not know what this alias is *for*. Advice that does not
        # apply to the declared intent is noise, and noise is what makes people
        # stop reading the advice at all.
        suppressed = set()
        if not declared.get("vision"):
            suppressed.add("projector_missing")  # text-only by design
        if model.spec.protocols.anthropic:
            suppressed.add("anthropic_via_translation")  # already exposed
        relevant = [
            _with_apply(a.to_dict(), probe, endpoint)
            for a in resolve_commands(probe.advice, endpoint.managed_by)
            if a.issue not in suppressed
        ]

        backends.append(
            {
                "endpoint": endpoint.name,
                "base_url": endpoint.normalized_base_url,
                "server_type": endpoint.server_type.value,
                "reachable": probe.reachable,
                "measured": probe.capabilities,
                "context_tokens": probe.context_tokens,
                "drift": drift,
                "advice": relevant,
                "notes": probe.notes,
            }
        )

    total_drift = sum(len(b["drift"]) for b in backends)
    blockers = sum(
        1 for b in backends for a in b["advice"] if a["severity"] == "blocker"
    )
    return {
        "model": alias,
        "declared_context_tokens": model.spec.limits.context_tokens,
        "backends": backends,
        "summary": {
            "backends": len(backends),
            "drift": total_drift,
            "blockers": blockers,
            "verdict": "blocked" if blockers else "drift" if total_drift else "consistent",
        },
    }


# ---------------------------------------------------------------------------
# Console-triggered test runs
# ---------------------------------------------------------------------------
@router.post("/models/{alias}/test", status_code=202)
async def start_model_test(
    alias: str,
    request: Request,
    only: str = Query("", description="comma-separated test ids"),
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Kick off MODEL-001..010 in the background and return a run id."""
    if alias not in state.registry.snapshot.models:
        raise GatewayError(ErrorCode.MODEL_NOT_FOUND, f"Unknown model '{alias}'.")

    selected = {t.strip().upper() for t in only.split(",") if t.strip()} or None
    run = ModelTestRun(
        model_alias=alias,
        status="running",
        actor_user_id=actor.user_id,
        total=len(selected or ModelTestSuite.ALL_TESTS),
        completed=0,
        results=[],
    )
    session.add(run)
    await audit(session, request, actor, "model.test", "model", alias)
    await session.commit()

    # The suite drives the public API, so it needs whatever the caller
    # authenticated with. A program sends a key; the console sends a session
    # cookie, and the public API accepts both. Reading only the bearer token
    # meant every run started from the console - the only place this button
    # exists - arrived unauthenticated and failed with MISSING_API_KEY.
    #
    # Either credential is the caller's own authority, held for the run and
    # never written anywhere.
    # extract_bearer_token raises MISSING_API_KEY rather than returning empty -
    # right where a key is required, wrong here, where a cookie is equally valid.
    # Calling it unguarded is what made the console button 401 before the run
    # even started, with the raised message showing up as the test result.
    try:
        api_key = extract_bearer_token(request)
    except GatewayError:
        api_key = ""
    session_cookie = "" if api_key else read_session_cookie(
        request.cookies, request.url.scheme == "https"
    )
    # Deliberately the server's own address, not request.base_url: the console
    # may be reached through a proxy or port-forward whose hostname means
    # nothing on this host.
    base_url = state.settings.self_base_url

    asyncio.create_task(
        _execute_test_run(run.id, alias, base_url, api_key, selected, session_cookie),
        name=f"model-test-{alias}",
    )
    return {"run_id": run.id, "model": alias, "status": "running"}


async def _execute_test_run(
    run_id: str,
    alias: str,
    base_url: str,
    api_key: str,
    only: set[str] | None,
    session_cookie: str = "",
) -> None:
    suite = ModelTestSuite(base_url, api_key, alias, session_cookie=session_cookie)
    collected: list[dict[str, Any]] = []

    async def on_result(result) -> None:  # noqa: ANN001
        collected.append(result.to_dict())
        async with session_scope() as session:
            run = await session.get(ModelTestRun, run_id)
            if run is not None:
                run.completed = len(collected)
                run.results = list(collected)

    try:
        results = await suite.run(only=only, progress=on_result)
        await suite.publish(results)
        async with session_scope() as session:
            run = await session.get(ModelTestRun, run_id)
            if run is not None:
                run.status = "done"
                run.completed = len(results)
                run.results = [r.to_dict() for r in results]
                run.finished_at = utcnow()
    except Exception as exc:
        log.exception("model test run %s failed", run_id)
        async with session_scope() as session:
            run = await session.get(ModelTestRun, run_id)
            if run is not None:
                run.status = "error"
                run.error = f"{type(exc).__name__}: {exc}"[:1000]
                run.finished_at = utcnow()
    finally:
        await suite.aclose()


@router.get("/test-runs/{run_id}")
async def get_test_run(
    run_id: str,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    run = await session.get(ModelTestRun, run_id)
    if run is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Unknown test run.")
    await _assert_may_read_model(session, actor, run.model_alias, state)
    return {
        "run_id": run.id,
        "model": run.model_alias,
        "status": run.status,
        "total": run.total,
        "completed": run.completed,
        "results": run.results or [],
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.get("/usage/summary")
async def usage_summary(
    days: int = Query(7, ge=1, le=365),
    workspace_id: str | None = None,
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    await state.usage.flush()  # include in-flight buffer in the report
    since = datetime.now(UTC) - timedelta(days=days)

    stmt = (
        select(
            UsageLog.model_alias,
            func.count(UsageLog.id),
            func.sum(UsageLog.text_input_tokens),
            func.sum(UsageLog.visual_input_tokens),
            func.sum(UsageLog.output_tokens),
            func.sum(UsageLog.image_count),
            func.avg(UsageLog.latency_ms),
            func.avg(UsageLog.ttft_ms),
        )
        .where(UsageLog.ts >= since)
        .group_by(UsageLog.model_alias)
    )
    if workspace_id:
        await _assert_owns(session, actor, workspace_id)
        stmt = stmt.where(UsageLog.workspace_id == workspace_id)
    visible = await _visible_users(session, actor)
    if visible is not None:
        stmt = stmt.where(UsageLog.user_id.in_(visible))
    rows = (await session.execute(stmt)).all()

    errors = (
        await session.execute(
            select(UsageLog.error_code, func.count(UsageLog.id))
            .where(
                UsageLog.ts >= since,
                UsageLog.status == "error",
                *([UsageLog.user_id.in_(visible)] if visible is not None else []),
            )
            .group_by(UsageLog.error_code)
        )
    ).all()

    return {
        "window_days": days,
        "by_model": [
            {
                "model": r[0],
                "requests": r[1],
                "text_input_tokens": int(r[2] or 0),
                "visual_input_tokens": int(r[3] or 0),
                "output_tokens": int(r[4] or 0),
                "images": int(r[5] or 0),
                "avg_latency_ms": round(float(r[6] or 0), 1),
                "avg_ttft_ms": round(float(r[7] or 0), 1) if r[7] else None,
            }
            for r in rows
        ],
        "errors": [{"code": e[0], "count": e[1]} for e in errors],
    }


@router.get("/usage/top-users")
async def top_users(
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(20, le=200),
    actor: Principal = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    visible = await _visible_users(session, actor)
    rows = (
        await session.execute(
            select(
                UsageLog.user_id,
                func.count(UsageLog.id),
                func.sum(UsageLog.total_tokens),
                func.sum(UsageLog.image_count),
            )
            .where(
                UsageLog.ts >= since,
                *([UsageLog.user_id.in_(visible)] if visible is not None else []),
            )
            .group_by(UsageLog.user_id)
            .order_by(func.sum(UsageLog.total_tokens).desc())
            .limit(limit)
        )
    ).all()
    user_ids = [r[0] for r in rows if r[0]]
    users = {}
    if user_ids:
        found = await session.execute(select(User).where(User.id.in_(user_ids)))
        users = {u.id: u for u in found.scalars()}
    return {
        "data": [
            {
                "user_id": r[0],
                "external_id": users[r[0]].external_id if r[0] in users else None,
                "display_name": users[r[0]].display_name if r[0] in users else None,
                "requests": r[1],
                "total_tokens": int(r[2] or 0),
                "images": int(r[3] or 0),
            }
            for r in rows
        ]
    }
