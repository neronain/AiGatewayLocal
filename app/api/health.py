"""Liveness, readiness and metrics.

/healthz  - process is up (no dependency checks; used by the container runtime)
/readyz   - registry loaded and at least one backend reachable (used by LB)
/metrics  - Prometheus exposition
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.core.auth import Principal, require_admin
from app.db.session import get_engine
from app.state import AppState, get_state

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(state: AppState = Depends(get_state)) -> dict[str, Any]:
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - state.started_at, 1),
        "models_loaded": len(state.registry.snapshot.models),
    }


@router.get("/readyz")
async def readyz(response: Response, state: AppState = Depends(get_state)) -> dict[str, Any]:
    snapshot = state.registry.snapshot
    health = state.router.health_report()
    healthy = [k for k, v in health.items() if v["healthy"]]

    db_ok = True
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    ready = bool(snapshot.models) and db_ok and bool(healthy)
    if not ready:
        response.status_code = 503
    return {
        "ready": ready,
        "database": "ok" if db_ok else "unavailable",
        "models_loaded": len(snapshot.models),
        "endpoints_healthy": len(healthy),
        "endpoints_total": len(health),
        "registry_errors": snapshot.errors,
    }


@router.get("/v1/health/endpoints")
async def endpoint_health(
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    return {"data": state.router.health_report()}


@router.post("/v1/health/probe")
async def probe_now(
    actor: Principal = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    await state.router.probe_all()
    return {"data": state.router.health_report()}


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
