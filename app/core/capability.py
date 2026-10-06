"""Capability validation (PRD §4, §14, §15).

The gateway rejects an impossible request itself instead of forwarding it and
letting vLLM/Ollama fail with a backend-shaped error. Two gates must pass:

    request  ->  model declares the capability?   ->  endpoint serves it?

Both are required. A vision-capable model pinned to a text-only backend cannot
serve images, and the failure must be a clean 400/503 from us, not a 500 from
somewhere downstream.
"""

from __future__ import annotations

from app.core import notices
from app.core.errors import ErrorCode, GatewayError, capability_error
from app.core.multimodal import RequestProfile
from app.core.tokens import estimate_prompt_tokens, rates_of
from app.registry.schema import Endpoint, Modality, ModelDefinition

# ประมาณจำนวน token เอง ไม่ได้รัน tokenizer ของโมเดล (PRD §13) จึงเผื่อไว้ก่อนปฏิเสธ
# app/core/rules.py ใช้ค่าเดียวกันตัดสินใจ overflow — แยกกันเมื่อไหร่คือกฎสองชุดที่ขัดกันเอง
CONTEXT_TOLERANCE = 1.15

# เพดานคำตอบขั้นต่ำที่ยังส่งไป เมื่อค่าประมาณบอกว่า prompt เต็มหรือเกินหน้าต่างแล้ว (โซน
# 100–115%) — พอให้ backend ตอบสั้น ๆ ได้ถ้า prompt จริงสั้นกว่าที่ประมาณ
MIN_OUTPUT_IN_DOUBT = 256

_CAPABILITY_HINTS = {
    "vision": "Choose a model whose badge shows 'Image', for example a vision model.",
    "tools": "Choose a model that lists 'Tools' among its capabilities.",
    "streaming": "Retry with \"stream\": false, or choose a streaming-capable model.",
    "audio": "Audio input is not available on this deployment yet.",
}


def validate_model_capabilities(model: ModelDefinition, profile: RequestProfile) -> None:
    """Gate 1 - does the model itself declare what the request needs?"""
    caps = model.spec.capabilities
    alias = model.alias

    if "image" in profile.modalities:
        if not caps.vision:
            raise capability_error(alias, "image input", _CAPABILITY_HINTS["vision"])
        if Modality.IMAGE not in model.spec.modalities.input:
            raise capability_error(alias, "image input", _CAPABILITY_HINTS["vision"])

    if "audio" in profile.modalities and not caps.audio:
        raise capability_error(alias, "audio input", _CAPABILITY_HINTS["audio"])

    if "video" in profile.modalities:
        raise capability_error(alias, "video input")

    if profile.requires_tools and not caps.tools:
        raise capability_error(alias, "tool calling", _CAPABILITY_HINTS["tools"])

    if profile.requires_streaming and not caps.streaming:
        raise capability_error(alias, "streaming", _CAPABILITY_HINTS["streaming"])

    if not caps.chat:
        raise capability_error(alias, "chat completions")

    _validate_translatable(model, profile)


def _speaks_natively(model: ModelDefinition, surface: str) -> bool:
    return any(
        e.enabled and getattr(e.protocols, surface, False) for e in model.spec.endpoints
    )


def _validate_translatable(model: ModelDefinition, profile: RequestProfile) -> None:
    """คำขอมีส่วนที่ chat completions แสดงไม่ได้ และโมเดลนี้เสิร์ฟ surface นี้ผ่านตัวแปลเท่านั้น

    เดิมตัวแปลทิ้งส่วนพวกนั้นเงียบ ๆ แล้วโมเดลตอบโดยไม่เห็นสิ่งที่ผู้ใช้ส่งมา · ตอบ 400 ที่
    ระบุตำแหน่งตรงนี้ — ก่อนโควตา ก่อนจองช่อง ก่อนเปิดสตรีม · โมเดลที่มีเครื่องพูด protocol
    นั้นเองผ่านด่านนี้ และ `endpoint_supports` จะกันไม่ให้คำขอไปลงเครื่องที่ต้องแปล

    นิยามเครื่องมือที่ตัวแปลจะ *ข้าม* ไม่ใช่เหตุให้ปฏิเสธ แต่ต้องบอก: header
    `x-litegate-ignored` (เฉพาะเมื่อโมเดลนี้ต้องแปลแน่ ๆ)
    """
    if not profile.surface or _speaks_natively(model, profile.surface):
        return
    if profile.native_only:
        path, why = profile.native_only[0]
        more = len(profile.native_only) - 1
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK,
            f"{path}: {why}." + (f" ({more} more part(s) like this.)" if more else ""),
            param=path,
            details={
                "model": model.alias,
                "unsupported": [where for where, _ in profile.native_only][:20],
            },
        )
    notices.put(notices.IGNORED, ", ".join(profile.skipped_in_translation[:20]) or None)


def validate_protocol(model: ModelDefinition, protocol: str) -> None:
    """Gate 1b - is this API surface enabled for the model?"""
    supported = getattr(model.spec.protocols, protocol, False)
    if not supported:
        raise GatewayError(
            ErrorCode.PROTOCOL_NOT_SUPPORTED,
            f"Model '{model.alias}' is not available over the {protocol} API. "
            f"Available: {', '.join(_enabled_protocols(model)) or 'none'}.",
            details={"model": model.alias, "requested_protocol": protocol},
        )


def _enabled_protocols(model: ModelDefinition) -> list[str]:
    # ต้องครบทุก surface · เดิมนับแค่สองตัว โมเดลที่เปิดเฉพาะ responses จึงถูกบอกว่า
    # "Available: none" ซึ่งอ่านแล้วเหมือนทะเบียนพัง ทั้งที่มันใช้ได้อยู่ แค่คนละทาง
    return [
        p
        for p in ("openai", "anthropic", "responses", "embeddings", "rerank")
        if getattr(model.spec.protocols, p, False)
    ]


def endpoint_supports(endpoint: Endpoint, profile: RequestProfile, protocol: str) -> bool:
    """Gate 2 - can this specific backend serve the request?"""
    if not endpoint.enabled:
        return False
    if not getattr(endpoint.protocols, protocol, False):
        return False
    # มีส่วนที่แปลไม่ได้ → เฉพาะเครื่องที่พูด protocol ของ surface ที่คำขอเข้ามาเอง · ไม่งั้น
    # failover จากเครื่อง native ไปเครื่อง openai-only จะพาคำขอไปถูกแปลแบบของหาย
    if profile.native_only and protocol != profile.surface:
        return False
    for modality in profile.modalities:
        try:
            if not endpoint.modalities.supports(Modality(modality)):
                return False
        except ValueError:
            return False
    return True


def validate_context_budget(
    model: ModelDefinition, profile: RequestProfile, requested_max_tokens: int | None
) -> int:
    """Reject over-long prompts up front and clamp max_tokens to the window.

    Returns the effective max output tokens. The prompt figure is an estimate -
    the gateway does not run the model's tokenizer (PRD §13) - so a safety margin
    is applied before rejecting, to avoid false negatives on borderline requests.

    Whenever the returned cap is lower than what the caller asked for (or, when
    they named none, lower than the model's own limit) the request is told so in
    the `x-litegate-output-cap` response header - see `_note_output_cap`.
    """
    limits = model.spec.limits
    estimated_prompt = estimate_prompt_tokens(profile, rates_of(model.spec))

    # สิ่งที่ผู้เรียกคาดว่าจะได้: ค่าที่เขาขอ หรือเพดานของโมเดลเมื่อไม่ได้ขอ
    expected = requested_max_tokens or limits.max_output_tokens
    max_output = min(expected, limits.max_output_tokens)
    reason = "model-limit" if max_output < expected else None

    # `n` คำตอบ = backend เขียน `max_tokens` *ต่อคำตอบ* · เพดาน `max_output_tokens` เป็นของ
    # ทั้งคำขอ (คือสิ่งที่แค็ตตาล็อกโชว์ และสิ่งที่โควตาตรวจล่วงหน้าไม่ได้) จึงแบ่งให้แต่ละ
    # คำตอบเท่า ๆ กัน · เดิม n=4 บนโมเดลเพดาน 16,384 ขอ output ได้ 65,536 ในคำขอเดียว
    ceiling = limits.max_output_tokens // max(profile.choices, 1)
    if ceiling < 1:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"'n' is {profile.choices}, but '{model.alias}' writes at most "
            f"{limits.max_output_tokens:,} output tokens per request - not enough for "
            "one token per choice. Ask for fewer choices.",
            param="n",
        )
    if ceiling < max_output:
        max_output, reason = ceiling, "n"

    # Only reject when the prompt is unambiguously too long.
    if estimated_prompt > limits.context_tokens * CONTEXT_TOLERANCE:
        raise GatewayError(
            ErrorCode.CONTEXT_LENGTH_EXCEEDED,
            f"Estimated prompt length (~{estimated_prompt:,} tokens) exceeds the "
            f"{limits.context_tokens:,}-token context window of '{model.alias}'.",
            details={
                "estimated_prompt_tokens": estimated_prompt,
                "context_tokens": limits.context_tokens,
            },
        )

    # ค่าประมาณอยู่ระหว่าง 100% ถึง 115% ของหน้าต่าง = "อาจจะพอดี" — ปล่อยผ่านให้ backend
    # ตัดสิน แต่ต้องไม่ขอคำตอบเต็มเพดานไปด้วย: เดิมกรณี headroom <= 0 ไม่ถูก clamp เลย
    # (ประมาณ 144,179 บนหน้าต่าง 131,072 ส่ง max_tokens 8192 ไปเต็ม ๆ) ซึ่งการันตีว่า
    # backend ที่ตรวจ prompt + max_tokens จะปฏิเสธ แม้ prompt จริงจะสั้นกว่าที่ประมาณ
    headroom = max(limits.context_tokens - estimated_prompt, MIN_OUTPUT_IN_DOUBT)
    if headroom < max_output:
        max_output, reason = headroom, "context"

    granted = max(max_output, 1)
    _note_output_cap(requested_max_tokens, granted, reason if granted < expected else None)
    return granted


def _note_output_cap(requested: int | None, granted: int, reason: str | None) -> None:
    """บอกผู้เรียกเมื่อเพดานคำตอบที่ส่งให้ backend ต่ำกว่าที่เขาคาด

    เดิมเงียบสนิท: client ขอ `max_tokens=4000` กับ prompt ที่ *ประมาณ* ได้ 100–115% ของหน้าต่าง
    backend ได้ 256 แล้วคำตอบถูกตัดกลางประโยค โดย header ทุกตัวเหมือนคำขอปกติ (ตรวจ
    2026-10-06 · prompt จริง 198,866 token = 76% ของหน้าต่าง แต่ประมาณได้เกิน 100%)

        x-litegate-output-cap: granted=256; requested=4000; reason=context

    `requested=default` = ผู้เรียกไม่ได้ระบุ จึงเทียบกับเพดานของโมเดล · `reason` คือด่าน
    *สุดท้าย* ที่ลดค่าลง:

        model-limit  ขอเกิน `limits.max_output_tokens` ของโมเดล
        n            เพดานของโมเดลถูกแบ่งให้ `n` คำตอบ
        context      prompt (ตามที่ประมาณ) เหลือที่ในหน้าต่างน้อยกว่าที่ขอ

    ถูกเรียกทุกครั้งที่คิดเพดาน รวมถึงเมื่อ fallback สลับโมเดล ค่าหลังสุดจึงเป็นของตัวที่
    เสิร์ฟจริง และถูกลบออกเมื่อโมเดลใหม่ไม่ได้ลดอะไร
    """
    if reason is None:
        notices.put(notices.OUTPUT_CAP, None)
        return
    asked = "default" if requested is None else str(requested)
    notices.put(notices.OUTPUT_CAP, f"granted={granted}; requested={asked}; reason={reason}")


def validate_batch_context(model: ModelDefinition, profile: RequestProfile) -> None:
    """ด่าน context ของคำขอที่แตกเป็นหลายงานย่อย (embeddings · rerank)

    วัด **งานย่อยที่ใหญ่ที่สุด** ไม่ใช่ผลรวม — และนี่คือความต่างที่สำคัญจาก
    `validate_context_budget` · backend รัน forward pass แยกกันต่อสตริง (หรือต่อคู่
    query+เอกสาร) ผลรวมของ batch จึงไม่เคยต้องอยู่ใน window เดียว · เอาผลรวมไปตรวจ
    เมื่อไหร่ = ปฏิเสธการ index เอกสาร 500 ชิ้นที่เครื่องรับไหวสบาย ๆ ซึ่งเป็นรูปร่าง
    ของงาน RAG ทุกงาน

    ไม่มี max_output_tokens ให้คืน: เส้นทางนี้ไม่มี output
    """
    limits = model.spec.limits
    if profile.largest_item_tokens > limits.context_tokens * CONTEXT_TOLERANCE:
        raise GatewayError(
            ErrorCode.CONTEXT_LENGTH_EXCEEDED,
            f"One item in this request is estimated at ~"
            f"{profile.largest_item_tokens:,} tokens, which exceeds the "
            f"{limits.context_tokens:,}-token context window of '{model.alias}'. "
            f"Split the long item and retry.",
            details={
                # ตัวเลขล้วน · ไม่มีเนื้อหาและไม่มีดัชนีของชิ้นที่ยาว เพราะลำดับของ
                # เอกสารก็เป็นข้อมูลของผู้ใช้อย่างหนึ่ง (PRD FR-28)
                "estimated_item_tokens": profile.largest_item_tokens,
                "context_tokens": limits.context_tokens,
                "items": profile.batch_items,
            },
        )


def upstream_model_for(model: ModelDefinition, endpoint: Endpoint) -> str:
    """The name this particular backend knows the model by (PRD §4.1)."""
    return endpoint.upstream_model or model.spec.upstream_model


def compatibility_badges(model: ModelDefinition) -> list[str]:
    """Short capability labels for the member catalogue (PRD §6)."""
    caps = model.spec.capabilities
    badges: list[str] = []
    if Modality.TEXT in model.spec.modalities.input:
        badges.append("Text")
    if caps.vision:
        badges.append("Image")
    if caps.audio:
        badges.append("Audio")
    if caps.coding:
        badges.append("Code")
    if caps.tools:
        badges.append("Tools")
    if caps.reasoning:
        badges.append("Reasoning")
    if caps.agentic:
        badges.append("Agent")
    # ป้ายของงานค้นคืน · สองตัวนี้ตอบคำถามที่คนทำ RAG ถามก่อนเสมอ ("ตัวไหนทำ index
    # ได้ ตัวไหนจัดอันดับได้") และมันไม่มีทางดูออกจากชื่อรุ่น
    if caps.embedding:
        badges.append("Embedding")
    if caps.rerank:
        badges.append("Rerank")
    return badges
