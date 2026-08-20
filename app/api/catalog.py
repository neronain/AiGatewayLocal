"""Member-facing catalogue and self-service usage (PRD §6, FR-38).

Models are grouped by purpose and described with plain-language badges. The
upstream repository name is never present in any response on this router.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, authenticate, permitted_aliases
from app.core.capability import compatibility_badges
from app.db.session import get_session
from app.registry.schema import ModelDefinition, Purpose
from app.state import AppState, get_state

router = APIRouter(prefix="/v1", tags=["catalog"])

# Section headings for the member catalogue, in display order.
_PURPOSE_LABELS: dict[Purpose, str] = {
    Purpose.GENERAL: "General AI",
    Purpose.VISION: "Vision AI",
    Purpose.CODING: "Coding AI",
    Purpose.REASONING: "Reasoning AI",
    Purpose.AGENT: "Agent AI",
    Purpose.FAST: "Fast AI",
    Purpose.EMBEDDING: "Embedding",
}


def _format_context(tokens: int) -> str:
    if tokens >= 1000:
        return f"{tokens // 1024}K Context" if tokens % 1024 == 0 else f"{tokens // 1000}K Context"
    return f"{tokens} Context"


def _member_entry(model: ModelDefinition) -> dict[str, Any]:
    claude_code = model.spec.agent_clients.get("claude_code")
    return {
        "id": model.alias,
        "name": model.metadata.display_name,
        "description": model.metadata.description,
        "badges": compatibility_badges(model),
        "context": _format_context(model.spec.limits.context_tokens),
        "context_tokens": model.spec.limits.context_tokens,
        "max_output_tokens": model.spec.limits.max_output_tokens,
        "claude_code_ready": bool(claude_code and claude_code.enabled and claude_code.tested),
        "supports_images": model.spec.capabilities.vision,
        "supports_tools": model.spec.capabilities.tools,
        "supports_streaming": model.spec.capabilities.streaming,
        "protocols": [
            p for p in ("openai", "anthropic") if getattr(model.spec.protocols, p)
        ],
    }


@router.get("/catalog")
async def catalog(
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """What this person can actually call, grouped for browsing.

    Filtered by the same rule that gates the call, and it says which rule: a
    catalogue that has quietly shrunk reads as models having disappeared, and
    the first guess is that the gateway is broken rather than that someone was
    added to a class.
    """
    snapshot = state.registry.snapshot
    permission = await permitted_aliases(session, principal, snapshot.gateway)
    grouped = snapshot.by_purpose(principal.role)
    sections = []
    for purpose, label in _PURPOSE_LABELS.items():
        models = [m for m in grouped.get(purpose, []) if permission.allows(m.alias)]
        if not models:
            continue
        sections.append(
            {
                "purpose": purpose.value,
                "title": label,
                "models": [_member_entry(m) for m in sorted(models, key=lambda m: m.alias)],
            }
        )
    return {
        "user": {"display_name": principal.display_name, "role": principal.role},
        "sections": sections,
        "access": {
            "restricted": permission.aliases is not None,
            "reason": permission.reason,
        },
    }


@router.get("/me/key")
async def me_key(
    principal: Principal = Depends(authenticate),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """ข้อเท็จจริงของ key ที่กำลังใช้เรียกอยู่ตอนนี้

    ไม่เปิดเผยอะไรที่คนถือ key ไม่ได้มีอยู่แล้ว — ตัว key เองเขามี ส่วนขอบเขตของมัน
    เขาชนเข้าอยู่ทุกวันเวลาถูกปฏิเสธ · การบอกตรง ๆ ว่า "ใบนี้จำกัดไว้แค่นี้ หมดอายุวันนี้"
    เปลี่ยนการเดาให้เป็นข้อมูล และลดการทักผู้ดูแลเพื่อถามเรื่องที่ระบบตอบเองได้

    **ไม่คืนตัว key และไม่คืน hash** — คืนแค่ prefix ที่ผู้ใช้เอาไว้เทียบว่าใบไหน
    """
    from app.db.models import ApiKey

    row = await session.get(ApiKey, principal.api_key_id)
    if row is None:
        # เข้ามาด้วย session ของคอนโซล ไม่ใช่ด้วย key
        return {"via": principal.via, "key": None}
    return {
        "via": principal.via,
        "key": {
            "prefix": row.key_prefix,
            "label": row.name or "",
            "issued_at": row.created_at.isoformat() if row.created_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            # ข้อจำกัดที่เขียนบนใบนี้เอง · ว่าง = ไม่จำกัดเพิ่มจากที่ workspace/role ให้
            "limited_to_models": list(principal.key_models),
            "limited_to_groups": list(principal.key_access_groups),
        },
    }


@router.get("/me/usage")
async def me_usage(
    days: int = Query(14, ge=1, le=90),
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """ตัวเองใช้ไปเท่าไรในช่วงที่ผ่านมา — แยกรายวันและรายโมเดล

    โควตาใน /v1/me บอกแค่ "หน้าต่างนี้เหลือเท่าไร" ซึ่งตอบไม่ได้ว่าหมดไปกับอะไร ·
    สมาชิกที่โดนปฏิเสธเพราะโควตาหมดต้องเห็นว่าตัวเองใช้ไปกับโมเดลไหน วันไหน
    ไม่ใช่รู้แค่ว่าหมดแล้ว

    เห็นเฉพาะของตัวเอง — กรองด้วย user_id ของ principal เสมอ ไม่มีพารามิเตอร์ให้ระบุคนอื่น
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from app.db.models import UsageLog

    await state.usage.flush()
    since = datetime.now(UTC) - timedelta(days=days)
    mine = UsageLog.user_id == principal.user_id

    daily = (
        await session.execute(
            select(
                func.date(UsageLog.ts),
                func.count(UsageLog.id),
                func.sum(UsageLog.text_input_tokens + UsageLog.visual_input_tokens),
                func.sum(UsageLog.output_tokens),
            )
            .where(mine, UsageLog.ts >= since)
            .group_by(func.date(UsageLog.ts))
            .order_by(func.date(UsageLog.ts))
        )
    ).all()

    by_model = (
        await session.execute(
            select(
                UsageLog.model_alias,
                func.count(UsageLog.id),
                func.sum(UsageLog.text_input_tokens + UsageLog.visual_input_tokens),
                func.sum(UsageLog.output_tokens),
            )
            .where(mine, UsageLog.ts >= since)
            .group_by(UsageLog.model_alias)
        )
    ).all()

    return {
        "window_days": days,
        "daily": [
            {"date": str(d), "requests": r, "input_tokens": int(i or 0),
             "output_tokens": int(o or 0)}
            for d, r, i, o in daily
        ],
        "by_model": sorted(
            [
                {"model": m, "requests": r, "input_tokens": int(i or 0),
                 "output_tokens": int(o or 0)}
                for m, r, i, o in by_model
            ],
            key=lambda x: x["requests"], reverse=True,
        ),
    }


@router.get("/me")
async def me(
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Who am I, and how much of my quota is left."""
    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, ""
    )
    usage = await state.quota.usage_snapshot(principal.user_id, limits)
    return {
        "user_id": principal.user_id,
        "external_id": principal.external_id,
        "display_name": principal.display_name,
        "role": principal.role,
        "workspace_id": principal.workspace_id,
        "quota": usage,
    }
