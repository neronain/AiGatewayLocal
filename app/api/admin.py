"""Admin plane: users, courses, keys, quota, registry, usage (FR-10..FR-19)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    Principal,
    extract_bearer_token,
    generate_api_key,
    require_admin,
    require_instructor,
)
from app.core.capability import compatibility_badges, upstream_model_for
from app.core.errors import ErrorCode, GatewayError
from app.core.modeltest import ModelTestSuite, probe_backend
from app.db.models import (
    ApiKey,
    AuditLog,
    Course,
    CourseModel,
    Enrollment,
    ModelCompatibility,
    ModelRecord,
    ModelTestRun,
    QuotaPolicy,
    UsageLog,
    User,
    utcnow,
)
from app.db.session import get_session, session_scope
from app.registry.writer import (
    delete_model,
    registry_writable,
    render_yaml,
    validate_definition,
    write_model,
)
from app.state import AppState, get_state

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


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
    role: str = "student"
    status: str = "active"


@router.post("/users", status_code=201)
async def create_user(
    payload: UserIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if payload.role not in {"student", "instructor", "admin"}:
        raise GatewayError(ErrorCode.INVALID_REQUEST, f"Unknown role '{payload.role}'.")
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
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    if role:
        stmt = stmt.where(User.role == role)
    result = await session.execute(stmt)
    return {"data": [_user_dict(u) for u in result.scalars()]}


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
# Courses
# ---------------------------------------------------------------------------
class CourseIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str
    term: str = ""


@router.post("/courses", status_code=201)
async def create_course(
    payload: CourseIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    existing = await session.execute(select(Course).where(Course.code == payload.code))
    if existing.scalar_one_or_none():
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, f"Course '{payload.code}' already exists."
        )
    course = Course(**payload.model_dump())
    session.add(course)
    await audit(session, request, actor, "course.create", "course", payload.code)
    await session.commit()
    return {"id": course.id, "code": course.code, "name": course.name, "term": course.term}


@router.get("/courses")
async def list_courses(
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await session.execute(select(Course).order_by(Course.code))
    return {
        "data": [
            {
                "id": c.id,
                "code": c.code,
                "name": c.name,
                "term": c.term,
                "status": c.status,
            }
            for c in result.scalars()
        ]
    }


@router.post("/courses/{course_id}/models")
async def set_course_models(
    course_id: str,
    payload: dict[str, list[str]],
    request: Request,
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Replace the allow-list of aliases for a course."""
    course = await session.get(Course, course_id)
    if course is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Course not found.")

    aliases = payload.get("models", [])
    known = set(state.registry.snapshot.models)
    unknown = [a for a in aliases if a not in known]
    if unknown:
        raise GatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Unknown model alias(es): {', '.join(unknown)}.",
            details={"known_models": sorted(known)},
        )

    await session.execute(delete(CourseModel).where(CourseModel.course_id == course_id))
    for alias in aliases:
        session.add(CourseModel(course_id=course_id, model_alias=alias, enabled=True))
    await audit(
        session, request, actor, "course.models.set", "course", course_id, {"models": aliases}
    )
    await session.commit()
    return {"course_id": course_id, "models": aliases}


@router.post("/courses/{course_id}/enroll")
async def enroll(
    course_id: str,
    payload: dict[str, Any],
    request: Request,
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user_id = payload.get("user_id")
    if not user_id or await session.get(User, user_id) is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Unknown user_id.")
    if await session.get(Course, course_id) is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Course not found.")
    existing = await session.execute(
        select(Enrollment).where(
            Enrollment.course_id == course_id, Enrollment.user_id == user_id
        )
    )
    if existing.scalar_one_or_none():
        return {"course_id": course_id, "user_id": user_id, "status": "already_enrolled"}
    session.add(
        Enrollment(
            course_id=course_id, user_id=user_id, role=payload.get("role", "student")
        )
    )
    await audit(session, request, actor, "course.enroll", "course", course_id, payload)
    await session.commit()
    return {"course_id": course_id, "user_id": user_id, "status": "enrolled"}


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
class ApiKeyIn(BaseModel):
    user_id: str
    course_id: str | None = None
    name: str = ""
    expires_in_days: int | None = 180
    scopes: list[str] = Field(default_factory=list)


@router.post("/api-keys", status_code=201)
async def create_api_key(
    payload: ApiKeyIn,
    request: Request,
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The plaintext key is returned exactly once and never stored."""
    user = await session.get(User, payload.user_id)
    if user is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Unknown user_id.")
    if user.role == "admin" and not actor.is_admin:
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE, "Only an admin can issue an admin key."
        )

    plaintext, prefix, digest = generate_api_key()
    expires_at = (
        utcnow() + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    api_key = ApiKey(
        user_id=payload.user_id,
        course_id=payload.course_id,
        name=payload.name,
        key_prefix=prefix,
        key_hash=digest,
        scopes=payload.scopes,
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
        "course_id": payload.course_id,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "warning": "Store this key now. It cannot be retrieved again.",
    }


@router.get("/api-keys")
async def list_api_keys(
    user_id: str | None = None,
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(ApiKey).order_by(ApiKey.created_at.desc()).limit(500)
    if user_id:
        stmt = stmt.where(ApiKey.user_id == user_id)
    result = await session.execute(stmt)
    return {
        "data": [
            {
                "id": k.id,
                "user_id": k.user_id,
                "course_id": k.course_id,
                "name": k.name,
                "key_prefix": k.key_prefix,
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
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "API key not found.")
    api_key.revoked_at = utcnow()
    await audit(session, request, actor, "apikey.revoke", "apikey", key_id)
    await session.commit()
    return {"id": key_id, "revoked": True}


# ---------------------------------------------------------------------------
# Quota policies
# ---------------------------------------------------------------------------
class QuotaPolicyIn(BaseModel):
    scope: str = "global"
    course_id: str | None = None
    user_id: str | None = None
    model_alias: str | None = None
    window: str = "day"
    max_requests: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_images: int = 0


@router.post("/quota-policies", status_code=201)
async def create_quota_policy(
    payload: QuotaPolicyIn,
    request: Request,
    actor: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if payload.window not in {"day", "month", "term"}:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "window must be day, month or term.")
    policy = QuotaPolicy(**payload.model_dump())
    session.add(policy)
    await audit(session, request, actor, "quota.create", "quota", payload.scope)
    await session.commit()
    return {"id": policy.id, **payload.model_dump()}


@router.get("/quota-policies")
async def list_quota_policies(
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await session.execute(select(QuotaPolicy).where(QuotaPolicy.enabled.is_(True)))
    return {
        "data": [
            {
                "id": p.id,
                "scope": p.scope,
                "course_id": p.course_id,
                "user_id": p.user_id,
                "model_alias": p.model_alias,
                "window": p.window,
                "max_requests": p.max_requests,
                "max_input_tokens": p.max_input_tokens,
                "max_output_tokens": p.max_output_tokens,
                "max_images": p.max_images,
            }
            for p in result.scalars()
        ]
    }


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
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
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
    apiVersion: str = "edullm.gateway/v1"
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
        # here is the thing that silently breaks students.
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
        relevant = [a.to_dict() for a in probe.advice if a.issue not in suppressed]

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

    # The suite drives the public API, so it needs a usable key. The caller's own
    # key is exactly the right authority; it is held in memory for the run only
    # and never written anywhere.
    api_key = extract_bearer_token(request)
    # Deliberately the server's own address, not request.base_url: the console
    # may be reached through a proxy or port-forward whose hostname means
    # nothing on this host.
    base_url = state.settings.self_base_url

    asyncio.create_task(
        _execute_test_run(run.id, alias, base_url, api_key, selected),
        name=f"model-test-{alias}",
    )
    return {"run_id": run.id, "model": alias, "status": "running"}


async def _execute_test_run(
    run_id: str,
    alias: str,
    base_url: str,
    api_key: str,
    only: set[str] | None,
) -> None:
    suite = ModelTestSuite(base_url, api_key, alias)
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
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    run = await session.get(ModelTestRun, run_id)
    if run is None:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Unknown test run.")
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
    course_id: str | None = None,
    actor: Principal = Depends(require_instructor),
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
    if course_id:
        stmt = stmt.where(UsageLog.course_id == course_id)
    rows = (await session.execute(stmt)).all()

    errors = (
        await session.execute(
            select(UsageLog.error_code, func.count(UsageLog.id))
            .where(UsageLog.ts >= since, UsageLog.status == "error")
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
    actor: Principal = Depends(require_instructor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                UsageLog.user_id,
                func.count(UsageLog.id),
                func.sum(UsageLog.total_tokens),
                func.sum(UsageLog.image_count),
            )
            .where(UsageLog.ts >= since)
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
