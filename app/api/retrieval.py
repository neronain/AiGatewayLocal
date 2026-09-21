"""Retrieval surfaces: `/v1/embeddings` and `/v1/rerank`.

ทำไมสอง endpoint นี้ถึงต้องมี
-----------------------------
LMDS deploy โมเดล embedding/rerank ได้อยู่แล้ว (ดู `bundles/qwen3-embedding-8b`,
`bundles/qwen3-reranker-4b`) แต่ก่อนมีไฟล์นี้ LiteGate ตั้งประตูหน้าให้มันไม่ได้ —
ลูกค้าที่ทำ RAG จึงต้องยิงตรงไปที่ backend: ไม่ผ่าน API key ไม่ผ่านโควตา ไม่มี usage log
ซึ่งเท่ากับส่วนที่หนักที่สุดของ RAG (การ index เอกสารทั้งกอง) เป็นส่วนเดียวที่ไม่มีใครดูแล

รูปแบบของ `/v1/rerank` — ยึดตามของจริง ไม่ใช่ตามสเปกที่ไม่มีอยู่
----------------------------------------------------------------
OpenAI **ไม่มี** `/v1/rerank` · ที่มีคือรูปแบบของ Cohere/Jina ซึ่ง vLLM และ TEI ทำตาม
เราเลือกยึดรูปแบบที่ backend ของเราเสิร์ฟจริง เพราะ `bundles/qwen3-reranker-4b/
MODEL_PROFILE.yaml` ระบุไว้ตรง ๆ ว่า `rerank.endpoints: [/v1/rerank, /v1/score]`
เกตเวย์จึงเป็น **ทางผ่าน ไม่ใช่ตัวแปล**: body เดิมไปทั้งก้อน เปลี่ยนแค่ชื่อโมเดล
ผลที่ตามมาคือ client ของ Cohere/Jina/LlamaIndex ที่ชี้มาที่เกตเวย์ได้คำตอบหน้าตาเดิม

`/v1/score` ยังไม่เปิด และ `/v2/rerank` (Cohere v2) ก็ยังไม่เปิด — v2 คืน `results`
คนละรูป การรับ path ของ v2 แล้วตอบ body ของ v1 คือการโกหกที่แพงกว่าการไม่รับ

สิ่งที่เส้นทางนี้จงใจ *ไม่* ทำ
------------------------------
* **ไม่ย้ายไปโมเดลอื่น** — ทั้งกฎ routing ระดับโมเดล (`resolve_route`) และ fallback
  ข้าม alias ถูกปิดไว้ · เวกเตอร์จากคนละโมเดลอยู่คนละปริภูมิ การล้มไปรุ่นสำรอง
  "เพื่อให้คำขอสำเร็จ" คือการทำให้ดัชนีของลูกค้าเสียเงียบ ๆ โดยไม่มี error ให้เห็น ·
  ล้มข้ามเครื่องของ alias เดิม (น้ำหนักชุดเดียวกัน) ยังทำ — นั่นคือ HA ที่ต้องการจริง
* **ไม่แคชคำตอบ** — response cache มีไว้สำหรับ chat ที่ถามซ้ำกันบ่อย ๆ
* **ไม่สตรีม** — สองเส้นทางนี้คืนผลเป็นก้อนเดียว
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openai import _read_json, _RequestContext, _resolve_model
from app.core import jsonio
from app.core.auth import Principal, assert_model_permitted, authenticate
from app.core.capability import (
    upstream_model_for,
    validate_batch_context,
    validate_protocol,
)
from app.core.errors import ErrorCode, GatewayError
from app.core.jsonio import FastJSONResponse
from app.core.multimodal import RequestProfile
from app.core.retrieval import (
    UPSTREAM_EMBEDDINGS_PATH,
    UPSTREAM_RERANK_PATH,
    profile_embeddings_request,
    profile_rerank_request,
    rewrite_model_name,
)
from app.core.routing import RETRYABLE_ERRORS, is_retryable_status
from app.core.tokens import TokenUsage, resolve_pooling_usage
from app.db.session import get_session, release_connection
from app.registry.schema import Endpoint
from app.state import AppState, get_state
from app.upstream import client as upstream

log = logging.getLogger(__name__)
router = APIRouter(tags=["retrieval"])

EMBEDDINGS_PATH = "/v1/embeddings"
RERANK_PATH = "/v1/rerank"

Profiler = Callable[[dict[str, Any]], RequestProfile]


@router.post(EMBEDDINGS_PATH)
async def embeddings(
    request: Request,
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
):
    return await _serve(
        request,
        await _read_json(request),
        principal,
        state,
        session,
        surface="embeddings",
        upstream_path=UPSTREAM_EMBEDDINGS_PATH,
        profiler=profile_embeddings_request,
    )


@router.post(RERANK_PATH)
async def rerank(
    request: Request,
    principal: Principal = Depends(authenticate),
    state: AppState = Depends(get_state),
    session: AsyncSession = Depends(get_session),
):
    return await _serve(
        request,
        await _read_json(request),
        principal,
        state,
        session,
        surface="rerank",
        upstream_path=UPSTREAM_RERANK_PATH,
        profiler=profile_rerank_request,
    )


async def _serve(
    request: Request,
    body: dict[str, Any],
    principal: Principal,
    state: AppState,
    session: AsyncSession,
    *,
    surface: str,
    upstream_path: str,
    profiler: Profiler,
) -> FastJSONResponse:
    """ลำดับเดียวกับ chat ทุกด่าน — ต่างกันแค่ตัวที่ *ไม่* มีบนเส้นทางนี้

        authenticate -> workspace policy -> resolve alias -> ตรวจ surface
        -> อ่านรูปร่างคำขอ -> context ต่อชิ้น -> quota -> เลือกเครื่อง
        -> ส่งต่อ -> บันทึกการใช้งาน

    ที่หายไปจาก chat: vision policy (ไม่มีภาพ) · กฎ routing ข้ามโมเดล (ดูหัวไฟล์) ·
    max_tokens (ไม่มี output) · streaming · response cache

    การเดินซ้ำทางเดิมไม่ใช่การก๊อป — มันคือเหตุผลที่คำขอ embedding ผ่านด่านเดียวกับ
    ทุกคน แทนที่จะกลายเป็นประตูหลังที่โควตาไม่เห็น
    """
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    started = time.perf_counter()

    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "'model' is required.", param="model"
        )

    model = _resolve_model(state, alias, principal)
    await assert_model_permitted(
        session, principal, alias, state.registry.snapshot.gateway
    )
    validate_protocol(model, surface)

    profile = profiler(body)
    validate_batch_context(model, profile)

    limits = await state.quota.resolve_limits(
        session, principal.user_id, principal.workspace_id, alias
    )
    await state.quota.check(principal.user_id, limits)
    # ด่านที่สอง: เพดานของ key ใบนี้เอง — เหมือน chat ทุกประการ ดูเหตุผลที่ api/openai.py
    key_limits = await state.quota.resolve_key_limits(session, principal.api_key_id)
    if key_limits is not None:
        await state.quota.check_key(principal.api_key_id, key_limits)

    # คืน connection ก่อนยิง upstream เหมือนเส้นทาง chat (ดู app/db/session.py)
    await release_connection(session)

    endpoint = state.router.select(model, profile, surface)

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
        client_agent=request.headers.get("user-agent", "")[:128],
        protocol=surface,
        # ดูหัวไฟล์: ล้มข้ามเครื่องได้ ล้มข้ามโมเดลไม่ได้
        allow_model_fallback=False,
    )

    def build(target: Endpoint) -> tuple[dict[str, Any], dict[str, str]]:
        payload = dict(body)
        payload["model"] = upstream_model_for(context.model, target)
        return payload, upstream.upstream_headers(target, dict(request.headers))

    data, endpoint = await _forward(build, context, upstream_path)

    rewrite_model_name(data, alias)
    usage = resolve_pooling_usage(profile, data.get("usage"))
    _augment_usage_payload(data, usage)
    await context.finalize(usage)

    return FastJSONResponse(
        content=data,
        headers={
            "x-request-id": request_id,
            "x-litegate-model": alias,
            "x-litegate-served-by": context.model.alias,
            "x-litegate-endpoint": endpoint.name,
            **(
                {"x-litegate-failed-over": ",".join(sorted(context.tried))}
                if context.tried
                else {}
            ),
        },
    )


async def _forward(
    build: Callable[[Endpoint], tuple[dict[str, Any], dict[str, str]]],
    ctx: _RequestContext,
    upstream_path: str,
) -> tuple[dict[str, Any], Endpoint]:
    """ถามเครื่องหนึ่ง ถ้าเครื่องนั้นไม่สบายก็ถามเครื่องถัดไปของ alias เดิม

    ยังไม่มีอะไรถึงผู้เรียกตรงนี้ การลองใหม่จึงมองไม่เห็นจากฝั่งเขา — เหมือน
    `_complete_chat` ทุกอย่าง ต่างแค่ว่าเมื่อเครื่องหมด จะไม่ข้ามไปโมเดลอื่น
    """
    state, alias = ctx.state, ctx.requested_alias

    while True:
        endpoint = ctx.endpoint
        payload, headers = build(endpoint)
        await state.router.acquire(alias, endpoint, ctx.request_id)
        try:
            response = await upstream.post_json(endpoint, upstream_path, payload, headers)
        except GatewayError as exc:
            state.router.report_failure(alias, endpoint, exc.message)
            if exc.code in RETRYABLE_ERRORS and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            await ctx.finalize(
                _no_usage(ctx),
                status="error",
                http_status=exc.http_status,
                error_code=exc.code,
            )
            raise
        finally:
            await state.router.release(alias, endpoint, ctx.request_id)

        if response.status_code >= 400:
            # ข้อความนี้ถูกส่ง *กลับให้ผู้เรียก* ใน details.upstream_detail (ตัดที่ 500
            # ตัวอักษร) เหมือนเส้นทาง chat · และ **ไม่ถูกเขียนลง log**: ตัวจัดการ error
            # เขียนแค่ code กับ message ส่วน report_failure เก็บแค่ "HTTP <status>"
            #
            # ที่ต้องรู้ไว้ (FR-28): error ของ pydantic ฝั่ง vLLM พ่นค่าที่ส่งเข้าไปกลับ
            # ออกมาได้ ซึ่งบนเส้นทางนี้คือข้อความของผู้ใช้ · ปลายทางคือคนที่ส่งมันมาเอง
            # จึงไม่ใช่การรั่วข้ามคน — แต่ถ้าจะปิดสนิท ส่ง "" แทน body_text ตรงจุดเดียวนี้
            body_text = response.text[:2000]
            state.router.report_failure(alias, endpoint, f"HTTP {response.status_code}")
            if is_retryable_status(response.status_code) and (nxt := ctx.another_endpoint()):
                ctx.retarget(nxt)
                continue
            error = upstream.upstream_error(endpoint, response.status_code, body_text)
            await ctx.finalize(
                _no_usage(ctx),
                status="error",
                http_status=error.http_status,
                error_code=error.code,
            )
            raise error

        state.router.report_success(alias, endpoint)
        break

    try:
        data = jsonio.loads(response.content)
    except json.JSONDecodeError as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_ERROR, "The model server returned a malformed response."
        ) from exc
    if not isinstance(data, dict):
        raise GatewayError(
            ErrorCode.UPSTREAM_ERROR, "The model server returned a malformed response."
        )
    return data, endpoint


def _no_usage(ctx: _RequestContext) -> TokenUsage:
    """คำขอที่ล้มเหลวยังต้องถูกนับ — backend อาจประมวลผลไปแล้วก่อนพัง

    ใช้ค่าประมาณของเราเพราะไม่มี usage จาก backend ให้ยึด · ติดป้าย `estimated`
    อัตโนมัติ รายงานจึงแยกออกได้ว่าแถวไหนวัดมาและแถวไหนเดา
    """
    return resolve_pooling_usage(ctx.profile, None)


def _augment_usage_payload(data: dict[str, Any], usage: TokenUsage) -> None:
    """เติมตัวเลขของเราเข้าไปโดยไม่ทำลายรูป usage ที่ client รู้จัก

    `output_tokens: 0` เขียนไว้ตรง ๆ ไม่ใช่เพราะยังไม่ได้ทำ — เส้นทางนี้ไม่มี output
    จริง ๆ · เขียนไว้เพื่อให้คนอ่านบิลไม่ต้องเดาว่าเลขหายไปไหน
    """
    existing = data.get("usage")
    if not isinstance(existing, dict):
        existing = {
            "prompt_tokens": usage.text_input_tokens,
            "total_tokens": usage.text_input_tokens,
        }
    existing["litegate"] = {
        "text_input_tokens": usage.text_input_tokens,
        "output_tokens": 0,
        "accounting": usage.accounting,
    }
    data["usage"] = existing
