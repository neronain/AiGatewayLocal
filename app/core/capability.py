"""Capability validation (PRD §4, §14, §15).

The gateway rejects an impossible request itself instead of forwarding it and
letting vLLM/Ollama fail with a backend-shaped error. Two gates must pass:

    request  ->  model declares the capability?   ->  endpoint serves it?

Both are required. A vision-capable model pinned to a text-only backend cannot
serve images, and the failure must be a clean 400/503 from us, not a 500 from
somewhere downstream.
"""

from __future__ import annotations

from app.core.errors import ErrorCode, GatewayError, capability_error
from app.core.multimodal import RequestProfile
from app.core.tokens import estimate_prompt_tokens
from app.registry.schema import Endpoint, Modality, ModelDefinition

# ประมาณจำนวน token เอง ไม่ได้รัน tokenizer ของโมเดล (PRD §13) จึงเผื่อไว้ก่อนปฏิเสธ
# app/core/rules.py ใช้ค่าเดียวกันตัดสินใจ overflow — แยกกันเมื่อไหร่คือกฎสองชุดที่ขัดกันเอง
CONTEXT_TOLERANCE = 1.15

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
    return [p for p in ("openai", "anthropic") if getattr(model.spec.protocols, p, False)]


def endpoint_supports(endpoint: Endpoint, profile: RequestProfile, protocol: str) -> bool:
    """Gate 2 - can this specific backend serve the request?"""
    if not endpoint.enabled:
        return False
    if not getattr(endpoint.protocols, protocol, False):
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
    """
    limits = model.spec.limits
    estimated_prompt = estimate_prompt_tokens(profile)
    max_output = requested_max_tokens or limits.max_output_tokens
    max_output = min(max_output, limits.max_output_tokens)

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

    headroom = limits.context_tokens - estimated_prompt
    if headroom > 0:
        max_output = min(max_output, max(headroom, 256))
    return max(max_output, 1)


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
    return badges
