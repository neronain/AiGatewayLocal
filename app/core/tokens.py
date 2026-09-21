"""Token accounting, including the visual split (PRD §10, FR-37).

The gateway does not run any tokenizer (PRD §13) - that is the model server's
job. So accounting works in two tiers:

  1. `upstream`  - the backend reported usage. Total prompt tokens are authoritative;
                   we split them into text vs visual using the image estimate below.
  2. `estimated` - the backend reported nothing (some streaming paths). Everything
                   is derived from character counts and image geometry.

Every usage row records which tier produced it, so reports never silently mix
measured and estimated numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.multimodal import ImageRef, RequestProfile

# Average characters per token for mixed Thai/English prompts. Thai is denser
# per character than English, so this sits below the common English ~4.0.
CHARS_PER_TOKEN = 3.2

# Tile model, matching how most vision encoders bill: the image is covered by
# 512x512 tiles, each worth TILE_TOKENS, plus a fixed thumbnail pass.
TILE_SIZE = 512
TILE_TOKENS = 170
BASE_IMAGE_TOKENS = 85

# Used when dimensions cannot be read from the header (e.g. WEBP).
DEFAULT_IMAGE_TOKENS = 850
# Assumed geometry for remote URLs, which we never fetch.
REMOTE_IMAGE_TOKENS = 1105


def estimate_image_tokens(image: ImageRef) -> int:
    """Visual tokens for one image, from header geometry only - no decoding."""
    if image.source == "url":
        return REMOTE_IMAGE_TOKENS
    if not image.width or not image.height:
        return DEFAULT_IMAGE_TOKENS

    width, height = image.width, image.height
    # Most encoders downscale to fit a 2048 box, then a 768 short side.
    if max(width, height) > 2048:
        scale = 2048 / max(width, height)
        width, height = int(width * scale), int(height * scale)
    if min(width, height) > 768:
        scale = 768 / min(width, height)
        width, height = int(width * scale), int(height * scale)

    tiles_x = -(-width // TILE_SIZE)  # ceil
    tiles_y = -(-height // TILE_SIZE)
    return BASE_IMAGE_TOKENS + TILE_TOKENS * max(tiles_x * tiles_y, 1)


def estimate_visual_tokens(profile: RequestProfile) -> int:
    return sum(estimate_image_tokens(img) for img in profile.images)


def estimate_text_tokens(profile: RequestProfile) -> int:
    """อักขระที่ต้องเดา บวกกับ token ที่ไม่ต้องเดา

    `pretokenized_tokens` ไม่ใช่ค่าประมาณ: /v1/embeddings รับ token id ตรง ๆ ได้
    และคนที่ส่ง id มาก็บอกจำนวนที่แน่นอนมาแล้ว · ไม่บวกตรงนี้ = คำขอที่ส่ง token id
    ถูกคิดเป็น 0 token ทั้งที่ backend รันเต็ม ๆ ซึ่งคือช่องโหว่โควตาที่เปิดทิ้งไว้
    """
    return int(profile.text_chars / CHARS_PER_TOKEN) + profile.pretokenized_tokens


def estimate_prompt_tokens(profile: RequestProfile) -> int:
    return estimate_text_tokens(profile) + estimate_visual_tokens(profile)


@dataclass
class TokenUsage:
    text_input_tokens: int = 0
    visual_input_tokens: int = 0
    output_tokens: int = 0
    accounting: str = "estimated"  # upstream | estimated

    @property
    def input_tokens(self) -> int:
        return self.text_input_tokens + self.visual_input_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def resolve_usage(profile: RequestProfile, upstream_usage: dict | None) -> TokenUsage:
    """Turn a backend usage object (or its absence) into the split we store.

    OpenAI-shaped backends report `prompt_tokens` / `completion_tokens`;
    Anthropic-shaped ones report `input_tokens` / `output_tokens`. Neither
    separates visual from text, so we attribute the estimated visual portion and
    treat the remainder as text.
    """
    visual_estimate = estimate_visual_tokens(profile)

    if upstream_usage:
        prompt = int(
            upstream_usage.get("prompt_tokens")
            or upstream_usage.get("input_tokens")
            or 0
        )
        completion = int(
            upstream_usage.get("completion_tokens")
            or upstream_usage.get("output_tokens")
            or 0
        )
        if prompt or completion:
            visual = min(visual_estimate, prompt) if prompt else visual_estimate
            return TokenUsage(
                text_input_tokens=max(prompt - visual, 0),
                visual_input_tokens=visual,
                output_tokens=completion,
                accounting="upstream",
            )

    return TokenUsage(
        text_input_tokens=estimate_text_tokens(profile),
        visual_input_tokens=visual_estimate,
        output_tokens=0,
        accounting="estimated",
    )


def resolve_pooling_usage(profile: RequestProfile, upstream_usage: dict | None) -> TokenUsage:
    """การนับของ /v1/embeddings และ /v1/rerank — เส้นทางที่ **ไม่มี output token เลย**

    แยกจาก `resolve_usage` เพราะกติกาการอ่าน `total_tokens` ต่างกันจนใช้ตัวเดียวกันไม่ได้:

    * chat: `total_tokens = prompt + completion` · ตีความเป็น input ไม่ได้เด็ดขาด
    * pooling: ไม่มี completion ให้บวก `total_tokens` จึง **คือ** input ทั้งก้อน

    ข้อนี้ไม่ใช่เรื่องทฤษฎี — vLLM ตอบ `/v1/rerank` ด้วย usage ที่มีแต่ `total_tokens`
    ไม่มี `prompt_tokens` · ถ้าเอา `resolve_usage` มาใช้ซ้ำ ตัวเลขที่ backend วัดมาจริง
    จะถูกทิ้งแล้วบันทึกค่าประมาณของเราแทน โดยติดป้ายว่า "estimated" — คือรายงานที่
    ดูเหมือนทำงานปกติแต่ตัวเลขมาจากคนละที่กับที่คิด

    visual_input_tokens เป็น 0 เสมอตามโครงสร้าง: ทั้งสอง surface รับแต่ข้อความ
    """
    if upstream_usage:
        prompt = int(
            upstream_usage.get("prompt_tokens")
            or upstream_usage.get("input_tokens")
            or upstream_usage.get("total_tokens")
            or 0
        )
        if prompt:
            return TokenUsage(
                text_input_tokens=prompt,
                visual_input_tokens=0,
                output_tokens=0,
                accounting="upstream",
            )

    return TokenUsage(
        text_input_tokens=estimate_text_tokens(profile),
        visual_input_tokens=0,
        output_tokens=0,
        accounting="estimated",
    )
