"""Liveness, readiness and metrics.

/healthz  - process is up (no dependency checks; used by the container runtime)
/readyz   - registry loaded and at least one backend reachable (used by LB)
/metrics  - Prometheus exposition
"""

from __future__ import annotations

import pathlib
import time
from typing import Any

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app import config
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
        # ลายเซ็นอยู่ใน endpoint ที่ทุก deployment เรียกใช้ — monitoring, load balancer,
        # และคนที่ curl ดูว่าเกตเวย์ตัวไหนตอบอยู่ · MIT บังคับให้คงประกาศลิขสิทธิ์ไว้
        "version": config.VERSION,
        "product": config.PRODUCT,
        "built_by": config.AUTHOR,
        "author_url": config.AUTHOR_URL,
        "license": config.LICENSE_NOTE,
        # เมื่อไหร่ที่หน้าคอนโซลถูกวางลงเครื่องนี้ครั้งล่าสุด · version เป็นเลขรุ่น
        # ของสินค้า ไม่ขยับตอน deploy ไฟล์หน้าเว็บ คนจึงไม่มีทางรู้ว่าที่เห็นอยู่
        # เป็นของใหม่หรือเบราว์เซอร์หยิบของเก่ามาแสดง
        "console_updated": _console_mtime(),
    }


def _console_mtime() -> str | None:
    """เวลาที่ไฟล์หน้าคอนโซลถูกแก้ล่าสุด (ISO, UTC) — None ถ้าหาไฟล์ไม่เจอ"""
    from datetime import datetime, timezone

    here = pathlib.Path(__file__).resolve().parent.parent / "static"
    stamps = [
        f.stat().st_mtime
        for name in ("index.html", "app.js", "style.css")
        if (f := here / name).exists()
    ]
    if not stamps:
        return None
    return datetime.fromtimestamp(max(stamps), tz=timezone.utc).isoformat()


async def _readiness(state: AppState) -> dict[str, Any]:
    """นิยามเดียวของคำว่า "พร้อม" — ใช้ทั้ง /readyz และ /metrics และตั้ง gauge ทุกครั้งที่ถูกเรียก

    เดิม gauge ถูกตั้งจากใน /readyz เท่านั้น · ไม่มีใครเรียก /readyz = ไม่มีใครตั้ง และ
    เกตเวย์รันหลาย worker ซึ่ง gauge เป็นของแต่ละ process — worker ที่ตอบ /metrics
    จึงมักไม่ใช่ตัวที่เพิ่งตอบ /readyz

    เคสจริง 2026-10-05 บน VM AiGateway (GW_WORKERS=4): /readyz ตอบ ready=true ·
    8 โมเดล · endpoint ดี 2 จาก 9 ขณะที่ /metrics ห้ารอบติดกันตอบ ready 0 · โมเดล 0 ·
    endpoint 0 จาก 0 — กฎเตือน `endpoints_healthy < endpoints_total` จึงไม่มีวันดัง
    (0 < 0 เป็นเท็จ) ทั้งที่ backend ล่มอยู่ 7 ตัว · ศูนย์เพราะดีกับศูนย์เพราะไม่เคยถูกวัด
    หน้าตาเหมือนกัน
    """
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

    from app.main import ENDPOINTS_HEALTHY, ENDPOINTS_TOTAL, MODELS_LOADED, READY

    READY.set(1 if ready else 0)
    ENDPOINTS_HEALTHY.set(len(healthy))
    ENDPOINTS_TOTAL.set(len(health))
    MODELS_LOADED.set(len(snapshot.models))

    return {
        "ready": ready,
        "database": "ok" if db_ok else "unavailable",
        "models_loaded": len(snapshot.models),
        "endpoints_healthy": len(healthy),
        "endpoints_total": len(health),
        "registry_errors": snapshot.errors,
    }


@router.get("/readyz")
async def readyz(response: Response, state: AppState = Depends(get_state)) -> dict[str, Any]:
    report = await _readiness(state)
    if not report["ready"]:
        response.status_code = 503
    return report


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
async def metrics(state: AppState = Depends(get_state)) -> Response:
    # วัดตอนถูก scrape ใน worker ตัวที่ตอบ — ไม่พึ่งว่ามีใครเรียก /readyz มาก่อน
    await _readiness(state)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
