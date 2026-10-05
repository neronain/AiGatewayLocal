"""OpenAI-compatible surface: /v1/models, /v1/chat/completions (FR-30..FR-35).

The pipeline, in the order the PRD specifies (§15):

    authenticate -> workspace policy -> resolve alias -> parse content blocks
    -> validate model capability -> validate vision policy -> context budget
    -> quota -> select compatible healthy endpoint -> forward -> record usage
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Collection
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auto as auto_mod
from app.core import jsonio, responsecache
from app.core import usage as usage_mod
from app.core.auth import (
    Principal,
    assert_model_permitted,
    authenticate,
    permitted_aliases,
)
from app.core.capability import (
    compatibility_badges,
    upstream_model_for,
    validate_context_budget,
    validate_model_capabilities,
    validate_protocol,
)
from app.core.errors import ErrorCode, GatewayError
from app.core.jsonio import FastJSONResponse
from app.core.multimodal import RequestProfile, profile_openai_request
from app.core.quota import Consumption
from app.core.routing import RETRYABLE_ERRORS, is_retryable_status
from app.core.rules import fallback_models, resolve_route
from app.core.tokens import TokenUsage, resolve_usage
from app.db.session import get_session, release_connection
from app.registry.schema import Endpoint, ModelDefinition
from app.state import AppState, get_state
from app.upstream import client as upstream
from app.upstream.sse import DONE, format_sse, iter_sse_payloads, parse_chunk


def _rate(ctx) -> float | None:
    """อัตราอักขระนอก ASCII ต่อ token ของโมเดลที่ *เสิร์ฟจริง*

    อ่านจาก ctx ตอนนั้น ไม่ใช่จำไว้ล่วงหน้า — fallback ระดับโมเดลเปลี่ยน ctx.model
    ระหว่างคำขอได้ ถ้าจำค่าของตัวแรกไว้ ยอดที่บันทึกจะเป็นของโมเดลที่ไม่ได้รัน
    """
    model = getattr(ctx, "model", None)
    spec = getattr(model, "spec", None)
    return getattr(spec, "wide_chars_per_token", None)

log = logging.getLogger(__name__)
router = APIRouter(tags=["openai"])

CHAT_PATH = "/v1/chat/completions"

# Addresses one request to one backend. Called once per attempt, because the
# upstream model name and the API key belong to the machine, not the request.
BuildRequest = Callable[[Endpoint], tuple[dict[str, Any], dict[str, str]]]

# เลือกเครื่องของโมเดลหนึ่ง โดยข้ามเครื่องที่ลองแล้ว · แต่ละ surface เลือกไม่เหมือนกัน:
# /v1/messages กับ /v1/responses รับได้ทั้งเครื่องที่พูด protocol นั้นเองและเครื่องที่ต้องแปล
# จึงต้องเป็นฟังก์ชันของ surface ไม่ใช่ `router.select(..., protocol)` ตรง ๆ
SelectEndpoint = Callable[[ModelDefinition, Collection[str]], Endpoint]


def select_or_fall_back(
    state: AppState,
    model: ModelDefinition,
    profile: RequestProfile,
    protocol: str,
    select: SelectEndpoint,
    request_id: str,
) -> tuple[ModelDefinition, Endpoint]:
    """เครื่องที่จะรับคำขอนี้ และโมเดลที่เครื่องนั้นเสิร์ฟ

    endpoint failover แก้ "เครื่องนี้ล่ม" · ตรงนี้แก้ "ทุกเครื่องของ alias นี้ล่ม" ซึ่งเดิม
    จบที่ 503 ทั้งที่โมเดลเทียบเท่าอาจว่างอยู่อีกเครื่อง · ตัวสำรองต้องรับคำขอรูปนี้ได้จริง
    (ดู rules.fallback_models) ไม่งั้นข้าม
    """
    try:
        return model, select(model, ())
    except GatewayError:
        for candidate in fallback_models(state.registry.snapshot, model, profile, protocol):
            try:
                endpoint = select(candidate, ())
            except GatewayError:
                continue
            log.warning(
                "no endpoint for %s; falling back to %s (request %s)",
                model.alias, candidate.alias, request_id,
            )
            return candidate, endpoint
        raise


@router.get("/v1/models")
async def list_models(
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """OpenAI-shaped catalogue. Members only ever see the alias (PRD §6).

    Filtered by the same rule that gates the call. Listing a model that would be
    refused is worse than not listing it: the client offers it, the person picks
    it, and the error arrives after they have written their prompt.
    """
    snapshot = state.registry.snapshot
    permission = await permitted_aliases(session, principal, snapshot.gateway)
    data = []
    for model in snapshot.visible_to(principal.role):
        if not permission.allows(model.alias):
            continue
        entry: dict[str, Any] = {
            "id": model.alias,
            "object": "model",
            "created": 0,
            "owned_by": "litegate",
            # Non-standard but harmless extras that OpenAI SDKs pass through.
            "display_name": model.metadata.display_name,
            "description": model.metadata.description,
            "purpose": [p.value for p in model.spec.purpose],
            "capabilities": model.spec.capabilities.model_dump(),
            "modalities": {
                "input": [m.value for m in model.spec.modalities.input],
                "output": [m.value for m in model.spec.modalities.output],
            },
            # surface ไหนใช้ alias นี้ได้บ้าง — Codex กับ Claude Code ไม่ได้คุย protocol
            # เดียวกัน การเดาเอาจากรายชื่อโมเดลแล้วยิงผิดทางคือได้ 400 หลังพิมพ์ prompt เสร็จ
            "protocols": [
                name
                for name in ("openai", "anthropic", "responses", "embeddings", "rerank")
                if getattr(model.spec.protocols, name, False)
            ],
            "context_window": model.spec.limits.context_tokens,
            "max_output_tokens": model.spec.limits.max_output_tokens,
            "badges": compatibility_badges(model),
        }
        if principal.is_admin:
            entry["upstream_model"] = model.spec.upstream_model
            entry["endpoints"] = [e.name for e in model.spec.endpoints]
        data.append(entry)
    return {"object": "list", "data": data}


@router.post(CHAT_PATH)
async def chat_completions(
    request: Request,
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
):
    return await run_chat(request, await _read_json(request), principal, state, session)


async def run_chat(
    request: Request,
    body: dict[str, Any],
    principal: Principal,
    state: AppState,
    session: AsyncSession,
):
    """The chat pipeline, callable with a body from anywhere.

    The assistant builds its own request and needs the identical treatment -
    capability gate, vision policy, context budget, quota, routing, usage. Going
    through this rather than reimplementing it is what stops the assistant from
    becoming a way around the rules that apply to everyone else.
    """
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    started = time.perf_counter()

    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "'model' is required.", param="model"
        )

    # model="auto" — ให้เกตเวย์เลือกเองจากรูปร่างของคำขอ
    #
    # ต้องมาก่อน _resolve_model เพราะ "auto" ไม่ใช่ alias ที่มีอยู่จริงใน registry ·
    # ตัวเลือกจำกัดอยู่แค่โมเดลที่สมาชิกคนนั้นใช้ได้อยู่แล้ว — auto ไม่ใช่ทางลัด
    # ข้ามสิทธิ์ที่แอดมินตั้งไว้
    auto_choice = None
    if alias == auto_mod.ALIAS:
        snapshot = state.registry.snapshot
        permission = await permitted_aliases(session, principal, snapshot.gateway)
        allowed = [
            m for m in snapshot.visible_to(principal.role)
            if m.spec.enabled and permission.allows(m.alias)
        ]
        # profile ต้องรู้ policy ของโมเดล แต่ยังไม่รู้ว่าโมเดลไหน — ใช้ policy ระดับเกตเวย์
        # ไปก่อนเพื่อดูรูปร่างคำขอ แล้วค่อย profile ใหม่ด้วย policy ตัวจริงหลังเลือกได้
        draft = profile_openai_request(body, snapshot.gateway.vision_policy)
        auto_choice = auto_mod.choose(
            allowed,
            profile=draft,
            protocol="openai",
            perf=state.perf,
        )
        if auto_choice is None:
            raise GatewayError(
                ErrorCode.MODEL_NOT_FOUND,
                "ไม่มีโมเดลที่คุณใช้ได้ตัวไหนรับคำขอรูปนี้ได้ "
                "(ลองระบุชื่อโมเดลตรง ๆ แทน auto)",
                param="model",
                details={"available_models": sorted(m.alias for m in allowed)},
            )
        alias = auto_choice.model.alias
        log.info("auto -> %s (%s, request %s)", alias, auto_choice.reason, request_id)

    model = _resolve_model(state, alias, principal)
    await assert_model_permitted(
        session, principal, alias, state.registry.snapshot.gateway
    )
    validate_protocol(model, "openai")

    policy = state.registry.snapshot.vision_policy_for(model)
    profile = profile_openai_request(body, policy)

    # กฎ routing ทำงานหลัง profile (ต้องรู้ขนาดคำขอ) แต่ก่อนด่าน capability/context
    # เพื่อให้ด่านตรวจ *ตัวที่จะรันจริง* · สิทธิ์กับโควตาเช็คไปแล้วด้วย alias เดิม ตามที่
    # app/core/rules.py อธิบายไว้ว่าทำไมถึงต้องเป็นแบบนั้น
    requested_max = body.get("max_tokens") or body.get("max_completion_tokens")
    decision = resolve_route(
        state.registry.snapshot, model, profile, "openai", requested_max
    )
    if decision.rerouted:
        log.info(
            "routing %s -> %s (%s, request %s)",
            alias, decision.model.alias, decision.reason, request_id,
        )
        model = decision.model

    validate_model_capabilities(model, profile)
    # ตรงนี้คือ "ปฏิเสธ prompt ที่ยาวเกิน" · เพดานคำตอบที่จะส่งไปจริงคิดใหม่ต่อโมเดลที่เสิร์ฟ
    # (context.output_cap) เพราะ fallback เปลี่ยนโมเดลได้หลังบรรทัดนี้
    validate_context_budget(model, profile, requested_max)

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, alias
    )
    await state.quota.check(principal.user_id, limits)
    # ด่านที่สอง: เพดานของ key ใบนี้เอง (ถ้ามีคนตั้งไว้) · ต้องผ่านทั้งสองด่าน —
    # ถ้าให้ด่านใดด่านหนึ่งชนะ การออก key ใบใหม่จะกลายเป็นวิธีขอโควตาเพิ่ม
    key_limits = await state.quota.resolve_key_limits(session, principal.api_key_id)
    if key_limits is not None:
        await state.quota.check_key(principal.api_key_id, key_limits)

    # The request's last read. Everything past this point - choosing an
    # endpoint, the upstream call, the stream itself, and the usage and quota
    # bookkeeping in ctx.finalize - runs without the request session, so give
    # the connection back now instead of when FastAPI tears the dependency down,
    # which for a stream is after the last SSE byte, minutes from here.
    # See app/db/session.py: release_connection.
    await release_connection(session)

    def select(target: ModelDefinition, exclude: Collection[str]) -> Endpoint:
        return state.router.select(target, profile, "openai", exclude=exclude)

    model, endpoint = select_or_fall_back(
        state, model, profile, "openai", select, request_id
    )

    # Rebuilt per attempt rather than once: the upstream model name and the API
    # key are properties of the machine, so a request that fails over has to be
    # re-addressed, not merely re-sent.
    def build(target: Endpoint) -> tuple[dict[str, Any], dict[str, str]]:
        # อ่านจาก context ไม่ใช่ตัวแปรปิด: fallback ระดับโมเดลเปลี่ยน ctx.model ได้
        # ระหว่างทาง ถ้ายังยึดตัวเดิมจะส่งชื่อ upstream ผิดไปให้เครื่องใหม่
        payload = dict(body)
        payload["model"] = upstream_model_for(context.model, target)
        # ส่งเพดานไปเสมอ แม้ client ไม่ได้ขอ — ดู _RequestContext.output_cap
        payload.pop("max_completion_tokens", None)
        payload["max_tokens"] = context.output_cap()
        return payload, upstream.upstream_headers(target, dict(request.headers))

    client_agent = request.headers.get("user-agent", "")[:128]

    context = _RequestContext(
        state=state,
        principal=principal,
        model=model,
        endpoint=endpoint,
        requested_alias=alias,
        profile=profile,
        limits_window=limits.window,
        rate_limited=limits.rate_limited,
        key_window=key_limits.window if key_limits else "",
        key_rate_limited=bool(key_limits and key_limits.rate_limited),
        request_id=request_id,
        started=started,
        client_agent=client_agent,
        protocol="openai",
        requested_max_tokens=requested_max,
        select=select,
    )

    if body.get("stream"):
        return await _stream_chat(build, context)
    return await _complete_chat(build, context)


# ---------------------------------------------------------------------------
# Shared request context + bookkeeping
# ---------------------------------------------------------------------------
# ── บันทึกการใช้งานให้รอดแม้ client จะตัดการเชื่อมต่อกลางทาง ──────────────────
#
# ช่องโหว่ที่ปิดตรงนี้ (ตรวจพบ 2026-09): ทั้ง codebase ไม่มี `asyncio.shield` เลยสักที่
# พอ client หลุดกลาง stream Starlette ยกเลิก task ของ request → บล็อก `finally` ที่เรียก
# `ctx.finalize()` รันใต้ `CancelledError` → **`await` ตัวแรกข้างใน finalize โยนทิ้งทันที**
# → `usage.submit()` และ `quota.record()` ไม่เคยรัน
#
# ผลคือ **ตัดการเชื่อมต่อ = ใช้ฟรี** · กด Ctrl-C ทุกครั้งที่ตอบใกล้จบแล้ว token ที่ backend
# เผาไปจริงจะไม่ถูกนับเข้าโควตาใครเลย · ผู้ใช้ที่ทำแบบนี้ไม่ต้องตั้งใจโกงด้วยซ้ำ — coding
# agent ที่ยกเลิก request เมื่อผู้ใช้พิมพ์ต่อ ก็ทำให้เกิดอาการนี้เองตลอดเวลา
#
# วิธีแก้: ย้าย finalize ไปเป็น task ของตัวเอง แล้ว `shield` ไว้ · เมื่อผู้เรียกถูกยกเลิก
# shield ปล่อย CancelledError กลับไปตามเดิม (ผู้เรียกจึงยังจบแบบที่ควรจะเป็น) แต่ task
# ข้างในวิ่งต่อจนบันทึกเสร็จ
#
# ต้องเก็บ strong reference ไว้ใน _PENDING — asyncio เก็บแค่ weak reference กับ task ที่
# กำลังรัน ถ้าไม่มีใครถือไว้ GC เก็บทิ้งกลางคันได้ และเราจะกลับไปเสียเงินเหมือนเดิม
# โดยที่เทสยังเขียว เพราะในเทส task สั้นเกินกว่าจะโดน GC
_PENDING: set[asyncio.Task] = set()

# กันไม่ให้ finalize ที่ค้างสะสมไม่รู้จบตอน backend ล่ม — ทิ้งได้ดีกว่าให้หน่วยความจำบวม
MAX_PENDING_FINALIZERS = 2048


async def finalize_even_if_cancelled(coro) -> None:
    """รัน coroutine ที่บันทึกการใช้งานให้จบ แม้ผู้เรียกจะถูก cancel ระหว่างทาง"""
    if len(_PENDING) >= MAX_PENDING_FINALIZERS:
        log.error("finalizer backlog เต็ม (%d) — ทิ้งการบันทึกรอบนี้", len(_PENDING))
        coro.close()
        return
    task = asyncio.create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # client หลุด · task ข้างในยังวิ่งต่อเพราะ shield — ปล่อยให้มันบันทึกจนจบ
        # แล้วส่ง CancelledError ต่อไปตามสัญญาของ asyncio
        raise


async def drain_pending_finalizers(timeout: float = 5.0) -> int:
    """รอให้ finalize ที่ค้างอยู่บันทึกจนครบ — เรียกตอนปิดแอป ไม่งั้นรอบสุดท้ายหาย"""
    pending = set(_PENDING)
    if not pending:
        return 0
    done, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        log.error("ปิดแอปโดยยังมี finalizer ค้าง %d ตัว — การใช้งานรอบนั้นไม่ถูกบันทึก",
                  len(still))
    return len(done)


class _RequestContext:
    def __init__(
        self,
        *,
        state: AppState,
        principal: Principal,
        model: ModelDefinition,
        endpoint: Endpoint,
        requested_alias: str | None = None,
        profile: RequestProfile,
        limits_window: str,
        rate_limited: bool,
        key_window: str = "",
        key_rate_limited: bool = False,
        request_id: str,
        started: float,
        client_agent: str,
        protocol: str,
        allow_model_fallback: bool = True,
        requested_max_tokens: int | None = None,
        select: SelectEndpoint | None = None,
    ) -> None:
        self.state = state
        self.principal = principal
        self.model = model
        # alias ที่สมาชิกขอ — ไม่เปลี่ยนตามการจัดเส้นทางภายใน · ทั้ง response ที่ตอบกลับ
        # และการบันทึกโควตาต้องยึดตัวนี้ ไม่งั้นบิลของสมาชิกจะขึ้นกับท่อของแอดมิน
        # และ client ที่ตรวจชื่อโมเดลที่ echo กลับมาจะพัง
        #
        # **ใช้กับเรื่องของสมาชิกเท่านั้น** · ทุกอย่างที่เป็นเรื่องของ *เครื่อง* — จองช่อง
        # คืนช่อง รายงานสำเร็จ/ล้มเหลว — ต้องใช้ `self.model.alias` (ตัวที่เสิร์ฟจริง) เพราะ
        # endpoint เป็นของโมเดลนั้น · เคยใช้ตัวนี้แทน ทราฟฟิกที่ถูก reroute หรือ fallback จึง
        # ได้ตัวนับอีกกอง และ llama.cpp 1 slot ได้รับ 2 คำขอ (ตรวจพบ 2026-10-05)
        self.requested_alias = requested_alias or model.alias
        # ใบจองช่องบน backend · สร้างเองต่อคำขอ ไม่ใช้ request_id เพราะตัวนั้นรับมาจาก
        # `x-request-id` ของ client ได้ — ค่าซ้ำ = ใบจองใบเดียวกันในที่เก็บที่ทุก worker ใช้ร่วม
        # ตัวนับไม่ขยับ และ release ของตัวใดตัวหนึ่งคืนช่องของทุกตัว
        self.lease = uuid.uuid4().hex
        self.endpoint = endpoint
        self.profile = profile
        self.limits_window = limits_window
        self.rate_limited = rate_limited
        # ว่าง = ใบนี้ไม่มีนโยบายของตัวเอง · ไม่ต้องนับกองที่สอง
        self.key_window = key_window
        self.key_rate_limited = key_rate_limited
        self.request_id = request_id
        self.started = started
        self.client_agent = client_agent
        self.protocol = protocol
        # ยอมให้ล้มไปโมเดล *อื่น* ได้ไหมเมื่อเครื่องของ alias นี้หมด
        #
        # chat ยอม — คำตอบจากรุ่นสำรองยังเป็นคำตอบ · /v1/embeddings ยอมไม่ได้เด็ดขาด:
        # เวกเตอร์จากคนละโมเดลอยู่คนละปริภูมิ เอามาเทียบระยะกับดัชนีเดิมไม่ได้ ผลคือ
        # RAG ที่ "ยังทำงาน" แต่ค้นเจอแต่ของมั่ว และไม่มี error ให้ใครเห็นเลย
        # (ดู app/api/retrieval.py — ที่นั่นตั้งค่านี้เป็น False)
        self.allow_model_fallback = allow_model_fallback
        # ที่ client ขอมา (None = ไม่ได้ระบุ) — เก็บค่าดิบไว้ ไม่ใช่ค่าที่ clamp แล้ว เพราะ
        # เพดานขึ้นกับโมเดลที่เสิร์ฟ และโมเดลนั้นเปลี่ยนได้ระหว่างคำขอ (ดู output_cap)
        self.requested_max_tokens = requested_max_tokens
        self._select_endpoint = select
        # Which backends this request has already burned. Not a count: the same
        # machine must never be handed the request twice, and it stays healthy
        # for two more strikes after the first failure.
        self.tried: set[str] = set()
        # alias ที่ไล่จนหมดเครื่องแล้ว — กัน fallback วนกลับมาตัวเดิม
        self.exhausted: set[str] = set()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def _select(self, model: ModelDefinition, exclude: Collection[str] = ()) -> Endpoint:
        """เลือกเครื่องด้วยวิธีของ surface ที่คำขอเข้ามา — ตัวเดียวกับที่ใช้เลือกครั้งแรก

        เดิม failover เรียก `router.select(..., self.protocol)` ตรง ๆ · บน /v1/messages กับ
        /v1/responses ค่านั้นคือ "anthropic"/"responses" ซึ่งตรงกับเครื่องที่พูด protocol
        นั้นเองเท่านั้น โมเดลที่เสิร์ฟผ่านตัวแปล (endpoint พูดแต่ openai — คือเกือบทุกตัวที่
        Claude Code ใช้อยู่) จึง **ไม่เคย failover ไปเครื่องที่สองเลย** ทั้งที่ chat ทำได้
        """
        if self._select_endpoint is not None:
            return self._select_endpoint(model, exclude)
        return self.state.router.select(model, self.profile, self.protocol, exclude=exclude)

    def output_cap(self) -> int:
        """`max_tokens` ที่จะส่งให้ backend — ของโมเดลที่เสิร์ฟ *ตอนนี้*

        สองเรื่องที่ทำให้ต้องคิดตรงนี้ ไม่ใช่คิดครั้งเดียวตอนต้นคำขอ:

        **ส่งเสมอ แม้ client ไม่ได้ขอ** · `limits.max_output_tokens` คือเพดานที่แค็ตตาล็อก
        โชว์ว่า "Max output N" · /v1/messages กับ /v1/responses ใส่ให้มาตลอด แต่
        /v1/chat/completions เคยใส่เฉพาะเมื่อ client ส่ง `max_tokens` มา — ไม่ส่งมา = backend
        เขียนได้จนเต็ม context: คำขอเดียวกินโควตา output ทั้งก้อน (โควตาตรวจก่อน บันทึกทีหลัง)
        และถือ slot ของ llama.cpp ไว้เป็นนาที · เพดานที่ข้ามได้ด้วยการไม่ส่งฟิลด์ไม่ใช่เพดาน

        โมเดล reasoning: token ที่ใช้คิดนับอยู่ในเพดานเดียวกัน ตั้ง `max_output_tokens`
        ให้พอทั้งคิดและตอบ ไม่งั้นได้ content ว่างกับ finish_reason "length"

        **คิดใหม่เมื่อโมเดลเปลี่ยน** · fallback พาไปโมเดลที่หน้าต่างแคบกว่าได้ · เพดานที่
        clamp ไว้กับโมเดลแรกจะรวมกับ prompt แล้วเกินหน้าต่างของตัวสำรอง และถูกปฏิเสธ
        """
        return validate_context_budget(self.model, self.profile, self.requested_max_tokens)

    def another_endpoint(self) -> Endpoint | None:
        """A backend for this alias that has not been tried yet, or None.

        None covers both "there is only one machine" and "we have been through
        all of them", which the caller treats the same way: stop and report the
        failure it already has.
        """
        self.tried.add(self.endpoint.name)
        try:
            return self._select(self.model, self.tried)
        except GatewayError:
            pass
        if not self.allow_model_fallback:
            return None
        # เครื่องของ alias นี้หมดแล้ว — ยังไม่ยอมแพ้ถ้ามีโมเดลสำรองที่รับได้
        # ยังอยู่ก่อนไบต์แรกเสมอ (ผู้เรียกเป็นคนคุม) คนใช้จึงไม่มีทางเห็นคำตอบซ้ำครึ่งอัน
        for candidate in fallback_models(
            self.state.registry.snapshot, self.model, self.profile, self.protocol
        ):
            if candidate.alias in self.exhausted:
                continue
            try:
                endpoint = self._select(candidate)
            except GatewayError:
                self.exhausted.add(candidate.alias)
                continue
            log.warning(
                "%s exhausted; falling back to %s (request %s)",
                self.model.alias, candidate.alias, self.request_id,
            )
            self.exhausted.add(self.model.alias)
            self.model = candidate
            self.tried = set()
            return endpoint
        return None

    def retarget(self, endpoint: Endpoint) -> None:
        log.info(
            "failing over %s: %s -> %s (request %s)",
            self.model.alias, self.endpoint.name, endpoint.name, self.request_id,
        )
        self.endpoint = endpoint

    async def finalize(
        self,
        usage: TokenUsage,
        *,
        ttft_ms: int | None = None,
        status: str = "success",
        http_status: int = 200,
        error_code: str | None = None,
    ) -> None:
        """Record usage + quota consumption exactly once per request.

        ห่อด้วย shield เพราะจุดเรียกเกือบทุกจุดอยู่ในบล็อก `finally` ซึ่งรันใต้
        `CancelledError` เมื่อ client หลุด · ไม่ห่อ = `await` ตัวแรกข้างในโยนทิ้ง
        แล้วไม่มีใครถูกหักโควตาเลย (ดู finalize_even_if_cancelled ข้างบน)

        ห่อไว้ที่นี่จุดเดียวเพราะทั้งสามโปรโตคอล (openai · anthropic · responses)
        ใช้คลาสนี้ร่วมกันและเรียก finalize รวมกัน 12 จุด — แก้ทีละจุดคือรอวันที่มีคน
        เพิ่มจุดที่ 13 แล้วลืม
        """
        # "exactly once" ใน docstring เดิมไม่เคยมีอะไรบังคับ · พอมี shield แล้วการ
        # เรียกซ้ำจะกลายเป็นการคิดเงินซ้ำจริง ๆ จึงต้องกันให้เป็นจริงตามที่เขียนไว้
        if getattr(self, "_finalized", False):
            log.warning("finalize ถูกเรียกซ้ำสำหรับ request %s — ข้ามรอบหลัง", self.request_id)
            return
        self._finalized = True
        await finalize_even_if_cancelled(
            self._record_usage(
                usage,
                ttft_ms=ttft_ms,
                status=status,
                http_status=http_status,
                error_code=error_code,
            )
        )

    async def _record_usage(
        self,
        usage: TokenUsage,
        *,
        ttft_ms: int | None = None,
        status: str = "success",
        http_status: int = 200,
        error_code: str | None = None,
    ) -> None:
        """งานบันทึกจริง — เรียกผ่าน finalize() เท่านั้น"""
        record = usage_mod.build_record(
            request_id=self.request_id,
            principal=self.principal,
            model_alias=self.requested_alias,
            protocol=self.protocol,
            profile=self.profile,
            usage=usage,
            endpoint_name=self.endpoint.name,
            stream=self.profile.requires_streaming,
            latency_ms=self.elapsed_ms,
            ttft_ms=ttft_ms,
            status=status,
            http_status=http_status,
            error_code=error_code,
            client_agent=self.client_agent,
        )
        await self.state.usage.submit(record)
        # ตัวเลขชุดเดียวกับที่บันทึกลง UsageLog — ใช้ต่อทันทีสำหรับจัดอันดับ auto
        # บันทึกด้วย alias ที่ *รันจริง* ไม่ใช่ที่สมาชิกขอ ไม่งั้นความเร็วของ coding-long
        # จะไปโผล่ในสถิติของ coding
        self.state.perf.record(
            self.model.alias,
            latency_ms=self.elapsed_ms,
            ttft_ms=ttft_ms,
            output_tokens=usage.output_tokens,
        )
        # export ด้วย ไม่ใช่เก็บไว้ดูย้อนหลังใน UsageLog อย่างเดียว (ดู main.TTFT)
        if ttft_ms is not None:
            from app.main import TTFT

            TTFT.labels(self.model.alias).observe(ttft_ms / 1000.0)
        await self.state.quota.record(
            self.principal.user_id,
            self.limits_window,
            rate_limited=self.rate_limited,
            api_key_id=self.principal.api_key_id,
            key_window=self.key_window,
            key_rate_limited=self.key_rate_limited,
            delta=Consumption(
                requests=1,
                text_input_tokens=usage.text_input_tokens,
                visual_input_tokens=usage.visual_input_tokens,
                output_tokens=usage.output_tokens,
                images=self.profile.image_count,
            ),
        )


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------
async def _complete_chat(build: BuildRequest, ctx: _RequestContext) -> FastJSONResponse:
    """Ask a backend, and if that one is unwell, ask the next one.

    Nothing has reached the caller yet at this point, so a retry is invisible to
    them - which is the whole difference between one machine going down and one
    conversation breaking.
    """
    state, alias = ctx.state, ctx.requested_alias

    # แคชคำตอบแบบตรงตัวเป๊ะ — เข้าเงื่อนไขน้อยมากโดยตั้งใจ (ดู core/responsecache.py)
    cache = getattr(state, "response_cache", None)
    cache_key = None
    if cache is not None:
        probe_payload, _ = build(ctx.endpoint)
        cache_key = responsecache.build_key(
            ctx.principal, alias=alias, upstream_model=ctx.model.alias,
            protocol=ctx.protocol, payload=probe_payload,
        )
        if cache_key is None:
            # "เปิดแคชแล้วทำไมไม่ hit สักที" เป็นคำถามแรกที่ทุกคนถาม และเดิมตอบไม่ได้เลย
            # เพราะเงื่อนไขแคบมากโดยตั้งใจ · บอกเหตุผลไปตรง ๆ จะได้ไม่ต้องเดา
            log.debug(
                "ไม่แคชคำขอ %s: %s",
                ctx.request_id, responsecache.cacheable_reason(probe_payload),
            )
        if cache_key is not None:
            hit = await cache.get(cache_key)
            if hit is not None:
                data = hit["data"]
                usage = resolve_usage(ctx.profile, data.get("usage"), _rate(ctx))
                # **ต้องหักโควตาเหมือนไม่ได้แคช** ไม่งั้นถามซ้ำได้ฟรีไม่จำกัด
                # ซึ่งเป็นช่องโหว่รายได้แบบเดียวกับที่ปิดไปใน 1.6.0 แค่คนละทาง
                await ctx.finalize(usage)
                return FastJSONResponse(
                    content=data,
                    headers={
                        "x-request-id": ctx.request_id,
                        "x-litegate-model": alias,
                        "x-litegate-served-by": ctx.model.alias,
                        # บอกให้เห็นว่าคำตอบนี้มาจากแคช ไม่ใช่เพิ่งคิดมา — ไล่ปัญหาได้
                        # และผู้ใช้ที่เจอคำตอบเดิมซ้ำจะรู้ว่าทำไม
                        "x-litegate-cache": "hit",
                    },
                )

    while True:
        endpoint, served = ctx.endpoint, ctx.model.alias
        payload, headers = build(endpoint)
        await state.router.acquire(served, endpoint, ctx.lease)
        try:
            response = await upstream.post_json(endpoint, CHAT_PATH, payload, headers)
        except GatewayError as exc:
            state.router.report_failure(served, endpoint, exc.message)
            if exc.code in RETRYABLE_ERRORS and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            await ctx.finalize(
                resolve_usage(ctx.profile, None, _rate(ctx)),
                status="error",
                http_status=exc.http_status,
                error_code=exc.code,
            )
            raise
        finally:
            await state.router.release(served, endpoint, ctx.lease)

        if response.status_code >= 400:
            body = response.text[:2000]
            state.router.report_http_error(served, endpoint, response.status_code)
            if is_retryable_status(response.status_code) and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            error = upstream.upstream_error(endpoint, response.status_code, body)
            await ctx.finalize(
                resolve_usage(ctx.profile, None, _rate(ctx)),
                status="error",
                http_status=error.http_status,
                error_code=error.code,
            )
            raise error

        state.router.report_success(served, endpoint)
        break

    try:
        # ไม่ใช้ response.json() เพราะมันเรียก json ของ stdlib ตายตัว · เรามีไบต์อยู่แล้ว
        data = jsonio.loads(response.content)
    except json.JSONDecodeError as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_ERROR, "The model server returned a malformed response."
        ) from exc

    # The member asked for the alias; never leak the upstream repository name.
    data["model"] = alias
    usage = resolve_usage(ctx.profile, data.get("usage"), _rate(ctx))
    _augment_usage_payload(data, usage)
    await ctx.finalize(usage)

    # เก็บเฉพาะคำตอบที่สำเร็จจริง — error ไม่แคช (ตรวจแล้วข้างบน: ถึงตรงนี้คือ 200 + JSON)
    if cache is not None and cache_key is not None:
        await cache.put(cache_key, {"data": data})

    return FastJSONResponse(
        content=data,
        headers={
            "x-request-id": ctx.request_id,
            "x-litegate-model": alias,
            "x-litegate-cache": "miss",
            # ตัวที่ *รันจริง* — ต่างจาก x-litegate-model เมื่อกฎ routing เปลี่ยนเส้นทาง
            # (coding -> coding-long เพราะคำขอยาวเกิน) · สัญญากับสมาชิกยังเหมือนเดิม
            # คือขอ alias ไหนได้ alias นั้น แต่เวลาไล่ปัญหาต้องรู้ว่าใครตอบ ไม่งั้นตัวเลข
            # เร็ว/ช้าที่วัดได้จะถูกโยงไปผิดโมเดล
            "x-litegate-served-by": ctx.model.alias,
            "x-litegate-endpoint": endpoint.name,
            # Names the machines that were tried and failed before this one, so
            # a slow reply has a visible reason rather than an unexplained one.
            **({"x-litegate-failed-over": ",".join(sorted(ctx.tried))} if ctx.tried else {}),
        },
    )


def _augment_usage_payload(data: dict[str, Any], usage: TokenUsage) -> None:
    """Expose the visual split without breaking the OpenAI usage shape."""
    existing = data.get("usage")
    if not isinstance(existing, dict):
        existing = {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
        }
    existing["litegate"] = {
        "text_input_tokens": usage.text_input_tokens,
        "visual_input_tokens": usage.visual_input_tokens,
        "accounting": usage.accounting,
    }
    data["usage"] = existing


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
async def _stream_chat(build: BuildRequest, ctx: _RequestContext) -> StreamingResponse:
    async def generator() -> AsyncIterator[bytes]:
        state, alias = ctx.state, ctx.requested_alias
        upstream_usage: dict | None = None
        ttft_ms: int | None = None
        status, error_code, http_status = "success", None, 200
        # Once a chunk has left for the caller, failing over would replay the
        # answer from the top and they would read it twice. Before that, the
        # switch is invisible - so this flag is the whole retry policy here.
        emitted = False

        try:
            while True:
                endpoint, served = ctx.endpoint, ctx.model.alias
                payload, headers = build(endpoint)
                # Ask for a final usage chunk so accounting stays authoritative.
                # If the caller did not want it, it is stripped before
                # forwarding so the shape matches what they asked for.
                client_wants_usage = bool(
                    (payload.get("stream_options") or {}).get("include_usage")
                )
                payload["stream_options"] = {
                    **(payload.get("stream_options") or {}),
                    "include_usage": True,
                }

                retry: Endpoint | None = None
                await state.router.acquire(served, endpoint, ctx.lease)
                try:
                    async with upstream.stream_json(
                        endpoint, CHAT_PATH, payload, headers
                    ) as response:
                        if response.status_code >= 400:
                            body = await upstream.read_error_body(response)
                            state.router.report_http_error(served, endpoint, response.status_code)
                            if not emitted and is_retryable_status(response.status_code):
                                retry = ctx.another_endpoint()
                            if retry is None:
                                error = upstream.upstream_error(
                                    endpoint, response.status_code, body
                                )
                                status = "error"
                                error_code, http_status = error.code, error.http_status
                                yield format_sse(jsonio.dumpb(error.to_openai(ctx.request_id)))
                                yield format_sse(DONE)
                                return
                        else:
                            state.router.report_success(served, endpoint)
                            async for _event, data in iter_sse_payloads(response.aiter_lines()):
                                if data.strip() == DONE:
                                    continue
                                chunk = parse_chunk(data)
                                if chunk is None:
                                    emitted = True
                                    yield format_sse(data)
                                    continue

                                if ttft_ms is None:
                                    ttft_ms = ctx.elapsed_ms

                                if isinstance(chunk.get("usage"), dict):
                                    upstream_usage = chunk["usage"]
                                    if not client_wants_usage and not chunk.get("choices"):
                                        continue  # usage-only chunk nobody asked for

                                chunk["model"] = alias
                                emitted = True
                                yield format_sse(jsonio.dumpb(chunk))

                            yield format_sse(DONE)
                            return

                except GatewayError as exc:
                    state.router.report_failure(served, endpoint, exc.message)
                    if not emitted and exc.code in RETRYABLE_ERRORS:
                        retry = ctx.another_endpoint()
                    if retry is None:
                        status, error_code, http_status = "error", exc.code, exc.http_status
                        yield format_sse(jsonio.dumpb(exc.to_openai(ctx.request_id)))
                        yield format_sse(DONE)
                        return
                except Exception as exc:  # client disconnect, backend reset, ...
                    log.exception("stream failed for request %s", ctx.request_id)
                    state.router.report_failure(served, endpoint, str(exc))
                    status, error_code, http_status = "aborted", ErrorCode.UPSTREAM_ERROR, 502
                    return
                finally:
                    await state.router.release(served, endpoint, ctx.lease)

                ctx.retarget(retry)
        finally:
            usage = resolve_usage(ctx.profile, upstream_usage, _rate(ctx))
            await ctx.finalize(
                usage,
                ttft_ms=ttft_ms,
                status=status,
                http_status=http_status,
                error_code=error_code,
            )

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",  # nginx must not buffer SSE
            "x-request-id": ctx.request_id,
            "x-litegate-model": ctx.requested_alias,
            # ตัวที่ *รันจริง* — ต่างจาก x-litegate-model เมื่อกฎ routing เปลี่ยนเส้นทาง
            # (coding -> coding-long เพราะคำขอยาวเกิน) · สัญญากับสมาชิกยังเหมือนเดิม
            # คือขอ alias ไหนได้ alias นั้น แต่เวลาไล่ปัญหาต้องรู้ว่าใครตอบ ไม่งั้นตัวเลข
            # เร็ว/ช้าที่วัดได้จะถูกโยงไปผิดโมเดล
            "x-litegate-served-by": ctx.model.alias,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "Request body must be valid JSON."
        ) from exc
    if not isinstance(body, dict):
        raise GatewayError(ErrorCode.INVALID_REQUEST, "Request body must be a JSON object.")
    return body


def _resolve_model(state: AppState, alias: str, principal: Principal) -> ModelDefinition:
    snapshot = state.registry.snapshot
    model = snapshot.models.get(alias)
    if model is None:
        available = sorted(m.alias for m in snapshot.visible_to(principal.role))
        raise GatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"Model '{alias}' does not exist. Available models: {', '.join(available)}.",
            param="model",
            details={"available_models": available},
        )
    if not model.spec.enabled:
        raise GatewayError(
            ErrorCode.MODEL_DISABLED,
            f"Model '{alias}' is currently disabled.",
            param="model",
        )
    if model not in snapshot.visible_to(principal.role):
        raise GatewayError(
            ErrorCode.MODEL_NOT_PERMITTED,
            f"Model '{alias}' is not available for your account.",
            param="model",
        )
    return model
