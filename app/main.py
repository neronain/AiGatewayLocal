"""Application entrypoint: middleware, error handling, lifespan."""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app import config
from app.api import admin, anthropic, assistant, auth, catalog, health, openai, responses, tools
from app.config import get_settings
from app.core.auth import generate_api_key
from app.core.errors import ErrorCode, GatewayError
from app.db.models import ApiKey, User
from app.db.session import dispose_db, init_db, session_scope
from app.state import build_state

log = logging.getLogger(__name__)

REQUESTS = Counter(
    "litegate_requests_total",
    "Gateway requests",
    ["path", "method", "status", "model"],
)
LATENCY = Histogram(
    "litegate_request_duration_seconds",
    "Request duration",
    ["path", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
IN_FLIGHT = Gauge("litegate_requests_in_flight", "Requests currently being served")
ERRORS = Counter("litegate_errors_total", "Gateway errors by code", ["code"])

# Readiness as numbers, not just as a status code. `up` catches a process that
# died; it cannot see a gateway that is running happily with an unreachable
# database or no healthy backend to route to, which is the outage members
# actually experience. Set from /readyz, so there is one definition of ready.
READY = Gauge("litegate_ready", "1 when the gateway is ready to serve")
ENDPOINTS_HEALTHY = Gauge("litegate_endpoints_healthy", "Backend endpoints passing health checks")
ENDPOINTS_TOTAL = Gauge("litegate_endpoints_total", "Backend endpoints configured")
MODELS_LOADED = Gauge("litegate_models_loaded", "Models in the registry")


class _RevalidatingStatic(StaticFiles):
    """Serve the console with `Cache-Control: no-cache`.

    Without this the browser keeps serving an old console after an upgrade -
    typically a new index.html against a stale stylesheet, which looks like a
    broken page rather than a caching problem. `no-cache` still allows a 304 via
    ETag, so this costs a conditional request, not a re-download.
    """

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _bootstrap_admin(app: FastAPI) -> None:
    """Create the first administrator on an empty database.

    Gives that account a console password as well as an API key, so a new
    deployment can be signed into with a username and password instead of
    someone pasting a key out of the log into a browser. Set GW_ADMIN_USER and
    GW_ADMIN_PASSWORD to choose them; otherwise a password is generated and
    printed once.
    """
    settings = get_settings()
    try:
        async with session_scope() as session:
            from app.core.passwords import hash_password

            existing = await session.execute(
                select(User).where(User.role == "admin").order_by(User.created_at, User.id)
            )
            admins = list(existing.scalars())
            if admins:
                # ซ่อมเมื่อ *ไม่มีผู้ดูแลคนไหนเลย* ที่เข้าคอนโซลได้ · deployment จริงมัก
                # มี admin หลายคน และการเจอคนที่ไม่มีรหัสผ่านหนึ่งคนไม่ได้แปลว่าใครเข้า
                # ไม่ได้ — ตั้งรหัสให้เขาทั้งที่คนอื่นเข้าได้อยู่แล้ว คือแตะระบบที่ไม่ได้พัง
                # แล้วพิมพ์ความลับออก log โดยไม่มีใครต้องการ
                if any(a.password_hash for a in admins):
                    return
                # เรียงตามเวลาสร้าง เพื่อให้ทุก worker เลือกคนเดียวกันเสมอ
                current = admins[0]
                # An administrator that predates password sign-in has no hash, so
                # nobody can reach the console at all — not a policy, just a gap
                # left by an upgrade. Give it one and announce it exactly the way
                # a fresh install does, because a credential nobody is told about
                # is the same as no credential.
                # uvicorn รัน 4 worker และทุกตัวรัน startup hook นี้ · ถ้าต่างคนต่าง
                # สุ่มแล้วเขียนทับกัน จะได้รหัสผ่านสี่อันพิมพ์ออก log แต่ใช้ได้อันเดียว
                # (อันที่ commit ทีหลังสุด) แล้วคนอ่าน log ก็หยิบอันแรกไปลอง — เจอจริง
                # ตอน deploy 2026-08-14 · เขียนแบบมีเงื่อนไขแล้วดู rowcount: ใครแพ้ก็เงียบ
                recovered = settings.admin_password or secrets.token_urlsafe(12)
                won = await session.execute(
                    update(User)
                    .where(User.id == current.id, or_(User.password_hash == "",
                                                      User.password_hash.is_(None)))
                    .values(password_hash=hash_password(recovered),
                            must_change_password=not settings.admin_password)
                )
                await session.commit()
                if not won.rowcount:
                    return          # worker อื่นตั้งไปแล้ว — ของเราไม่ได้ถูกใช้ ห้ามพิมพ์
                log.warning(
                    "\n%s\nCONSOLE SIGN-IN RESTORED (shown once)\n"
                    "  This administrator existed without a password, which no\n"
                    "  release before console sign-in could set.\n"
                    "  username: %s\n  password: %s\n%s",
                    "=" * 72, current.external_id, recovered, "=" * 72,
                )
                return

            password = settings.admin_password or secrets.token_urlsafe(12)
            admin_user = User(
                external_id=settings.admin_user or "admin",
                display_name="Administrator",
                role="admin",
                password_hash=hash_password(password),
                # Generated passwords must be replaced; a chosen one is the
                # operator's own decision.
                must_change_password=not settings.admin_password,
            )
            session.add(admin_user)
            await session.flush()

            if settings.bootstrap_admin_key:
                from app.core.auth import PREFIX_LEN, hash_api_key

                plaintext = settings.bootstrap_admin_key
                prefix, digest = plaintext[:PREFIX_LEN], hash_api_key(plaintext)
            else:
                plaintext, prefix, digest = generate_api_key()

            session.add(
                ApiKey(
                    user_id=admin_user.id,
                    name="bootstrap",
                    key_prefix=prefix,
                    key_hash=digest,
                    scopes=["admin"],
                )
            )
    except IntegrityError:
        # Every worker runs this at startup; the unique constraint on
        # external_id makes the losers of the race fail here. That is the
        # constraint doing its job - exactly one bootstrap admin exists.
        log.info("bootstrap admin already created by another worker")
        return

    rule = "=" * 72
    log.warning(
        "\n%s\nCONSOLE SIGN-IN (shown once)\n  username: %s\n  password: %s\n%s",
        rule,
        settings.admin_user or "admin",
        "(the one you set in GW_ADMIN_PASSWORD)" if settings.admin_password else password,
        rule,
    )
    log.warning(
        "\n%s\nBOOTSTRAP ADMIN KEY (shown once): %s\n"
        "Store it now, then create real admin accounts and revoke this key.\n%s",
        rule,
        plaintext,
        rule,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    log.info("starting LiteGate (env=%s)", settings.env)

    if settings.is_production and settings.api_key_pepper == "dev-only-insecure-pepper":
        raise RuntimeError(
            "GW_API_KEY_PEPPER must be set to a unique secret in production."
        )

    await init_db()
    state = build_state()
    app.state.services = state
    state.started_at = time.time()
    await state.start()
    await _bootstrap_admin(app)

    # Mirror the YAML registry into the DB so usage joins and compat rows work.
    # `alias` is unique, so concurrent workers race here too; the loser's rows
    # are already present, which is the outcome we wanted anyway.
    try:
        async with session_scope() as session:
            await admin.sync_model_projection(session, state)
    except IntegrityError:
        log.info("model projection already synced by another worker")

    try:
        yield
    finally:
        log.info("shutting down")
        await state.stop()
        from app.upstream.client import close_client

        await close_client()
        await dispose_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="LiteGate",
        version=config.VERSION,
        description=(
            "Capability-aware, multimodal AI gateway for education. "
            "OpenAI-compatible and Anthropic-compatible."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["x-request-id", "x-litegate-model", "x-litegate-endpoint"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        IN_FLIGHT.inc()
        try:
            response = await call_next(request)
        finally:
            IN_FLIGHT.dec()
        duration = time.perf_counter() - started

        path = request.scope.get("route").path if request.scope.get("route") else "unmatched"
        model = response.headers.get("x-litegate-model", "-")
        REQUESTS.labels(path, request.method, str(response.status_code), model).inc()
        LATENCY.labels(path, model).observe(duration)
        response.headers["x-request-id"] = request_id
        # ลายเซ็นติดไปกับ *ทุก* response — ที่เดียวจบ ไม่ต้องไล่แก้ทุก endpoint
        # และถอดออกทีเดียวไม่ได้โดยที่ไม่มีใครสังเกต เพราะ client ที่ log header
        # ไว้จะเห็นมันหายไป · MIT ให้ fork ได้ แต่บังคับให้คงประกาศลิขสิทธิ์
        response.headers["x-litegate-by"] = config.AUTHOR
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        ERRORS.labels(exc.code).inc()
        request_id = getattr(request.state, "request_id", None)
        # Anthropic clients expect Anthropic's error envelope.
        is_anthropic = request.url.path.startswith("/v1/messages")
        body = exc.to_anthropic(request_id) if is_anthropic else exc.to_openai(request_id)
        headers = {"x-request-id": request_id} if request_id else {}
        if exc.retry_after:
            headers["retry-after"] = str(exc.retry_after)
        log.info(
            "rejected %s %s: %s (%s)",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
        )
        return JSONResponse(status_code=exc.http_status, content=body, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        ERRORS.labels(ErrorCode.INVALID_REQUEST).inc()
        error = GatewayError(
            ErrorCode.INVALID_REQUEST,
            "Request validation failed.",
            details={"errors": exc.errors()[:10]},
        )
        return JSONResponse(
            status_code=400,
            content=error.to_openai(getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        ERRORS.labels(ErrorCode.INTERNAL_ERROR).inc()
        request_id = getattr(request.state, "request_id", None)
        log.exception("unhandled error on %s (request_id=%s)", request.url.path, request_id)
        error = GatewayError(
            ErrorCode.INTERNAL_ERROR,
            "An internal error occurred. Quote the request id when reporting it.",
        )
        return JSONResponse(status_code=500, content=error.to_openai(request_id))

    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(openai.router)
    app.include_router(anthropic.router)
    app.include_router(responses.router)
    app.include_router(assistant.router)
    app.include_router(catalog.router)
    app.include_router(admin.router)
    app.include_router(tools.router)

    static_dir = settings.config_dir.parent / "app" / "static"
    if static_dir.is_dir():
        app.mount(
            "/console", _RevalidatingStatic(directory=str(static_dir), html=True), name="console"
        )

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        """หน้าต้อนรับสำหรับคน · JSON เดิมสำหรับเครื่อง

        เดิมคืน JSON ให้ทุกคน — คนที่เปิดที่อยู่ของเกตเวย์ในเบราว์เซอร์เป็นครั้งแรกจึงเจอ
        ก้อน JSON ที่ไม่บอกว่าจะไปต่อทางไหน · แต่ script และ monitoring ที่ยิงมาที่ `/`
        อยู่แล้วต้องไม่พัง จึงแยกด้วย Accept: ขอ HTML มา = ได้หน้า อย่างอื่น = ได้ JSON เดิม
        """
        wants_html = "text/html" in request.headers.get("accept", "")
        page = settings.config_dir.parent / "app" / "static" / "welcome.html"
        if wants_html and page.is_file():
            return FileResponse(page, media_type="text/html; charset=utf-8")
        return {
            "service": "LiteGate",
            "version": config.VERSION,
            "docs": "/docs",
            "console": "/console",
            "health": "/healthz",
        }

    return app


app = create_app()


def run() -> None:
    """Console-script entrypoint (`litegate`)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=settings.workers if settings.is_production else 1,
    )


if __name__ == "__main__":
    run()
