"""Member-facing catalogue and self-service usage (PRD §6, FR-38).

Models are grouped by purpose and described with plain-language badges. The
upstream repository name is never present in any response on this router.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
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
