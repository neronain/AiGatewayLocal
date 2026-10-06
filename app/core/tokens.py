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

from app.core.multimodal import _ASCII_SYMBOL, ImageRef, RequestProfile

# อักขระต่อ token แยกตามชนิด — **ค่าเดียวครอบไม่ได้**
#
# วัดจริงกับ `qwen3-embedding-8b` บน dgx-spark04 (2026-09-22) ด้วยข้อความ 8 ชุด
# แล้วอ่าน `usage.prompt_tokens` ที่ backend รายงานกลับมา:
#
#     อังกฤษล้วน        4.86 อักขระ/token
#     โค้ด              3.26
#     ไทยปนอังกฤษ       2.43
#     ไทยล้วน           1.89
#     ตัวเลข/สัญลักษณ์   1.16   (IP, เวอร์ชัน, วันที่ — เกือบ 1 token ต่ออักขระ)
#
# ค่าเดิมคือ `3.2` ตัวเดียว ซึ่งตั้งไว้โดยตั้งใจให้ต่ำกว่าอังกฤษเพราะรู้ว่ามีภาษาไทย —
# แต่ยังสูงไป **2.6 เท่า** สำหรับไทย · ผลคือยอดรวมของชุดทดสอบออกมาเป็น **73% ของจริง**
# และต่ำกว่าจริงใน 5 จาก 8 เคส
#
# ค่าด้านล่างให้ 107% ของจริง — **ตั้งใจให้เกินเล็กน้อย** เพราะนี่เป็นค่าสำรองที่ใช้ตอน
# backend ไม่รายงาน usage มา และในงานโควตา **การนับต่ำกว่าจริงคือช่องโหว่** ส่วนการนับ
# เกินเล็กน้อยแค่เข้มกับผู้ใช้เกินไปหน่อย · สองอย่างนี้ไม่เท่ากัน
CHARS_PER_TOKEN = 4.0          # ASCII ที่เป็นตัวอักษรหรือช่องว่าง
SYMBOL_CHARS_PER_TOKEN = 1.0   # ASCII อื่น — ตัวเลข วรรคตอน เครื่องหมาย
WIDE_CHARS_PER_TOKEN = 1.6     # นอก ASCII — ไทย จีน ญี่ปุ่น เกาหลี อีโมจิ

# ── ทำไมค่าตัวนอก ASCII ต้องตั้งต่อโมเดลได้ ─────────────────────────────────
#
# 1.6 ข้างบนเป็น **ค่าสำรอง** ไม่ใช่ความจริงของทุกโมเดล · วัดจริงบนฟลีต 2026-09-28
# ภาษาไทยกระจายตัว **1.80–3.86 อักขระ/token = ต่างกัน 2.1 เท่า** ระหว่างโมเดล
# เพราะ pre-tokenizer ของบางตัวไม่นับเครื่องหมายประสม (สระบน/ล่าง วรรณยุกต์)
# จึงฉีก "ที่" เป็น "ท" + "ี่" ส่วนตัวที่ถูกต้องนับเป็น token เดียว
#
# ผลของการใช้ค่าเดียว: เดาเกิน **+10%** กับโมเดลที่ tokenizer ฉีก (พอรับได้) แต่
# **+137%** กับโมเดลที่ถูกต้อง · ค่านี้คุมทั้งด่าน context และยอดโควตา ผู้ใช้ภาษาไทย
# บนโมเดลกลุ่มหลังจึงชนเพดานที่ ~42% ของความจุจริง และเผาโควตาเร็วกว่าที่ควร 2.4 เท่า
#
# ตั้งค่าได้ที่ `spec.wide_chars_per_token` ใน YAML ของโมเดล · ไม่ตั้ง = ใช้ค่าสำรอง
# ปุ่ม Detect ในคอนโซล (`POST /admin/models/detect`) วัดจาก tokenizer ของ backend ให้เอง
# ด้วย `app.core.modeltest.measure_wide_rate` · วัดมือได้ตาม docs/DEPLOYMENT.md §4.1b


def wide_rate(rate: float | None) -> float:
    """อัตราที่จะใช้จริง — ค่าของโมเดลถ้ามี ไม่มีก็ค่าสำรอง

    กันค่าที่เป็นไปไม่ได้ออกให้หมดตรงนี้ที่เดียว: 0 หรือติดลบจะทำให้หารพัง และค่าที่
    สูงเกินจริงคือการนับต่ำกว่าจริง ซึ่งในงานโควตาคือช่องโหว่ ไม่ใช่แค่ความคลาดเคลื่อน
    """
    if rate is None:
        return WIDE_CHARS_PER_TOKEN
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return WIDE_CHARS_PER_TOKEN
    if not 0.2 <= value <= 20.0:
        return WIDE_CHARS_PER_TOKEN
    return value

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


def estimate_chars(text: str, rate: float | None = None) -> int:
    """ประมาณ token ของข้อความชิ้นเดียว — ตัวเดียวกับที่ `estimate_text_tokens` ใช้

    มีไว้ให้ฝั่งที่ถือ *ตัวข้อความ* อยู่ (เช่นด่านตรวจ context ต่อชิ้นของ /v1/rerank)
    เรียกได้โดยไม่ต้องสร้าง `RequestProfile` ขึ้นมาทั้งก้อน
    """
    if not text:
        return 0
    plain = text.encode("ascii", "ignore").decode("ascii")
    wide = len(text) - len(plain)
    symbols = len(_ASCII_SYMBOL.findall(plain))
    letters = len(plain) - symbols
    return (int(letters / CHARS_PER_TOKEN)
            + int(symbols / SYMBOL_CHARS_PER_TOKEN)
            + int(wide / wide_rate(rate)))


def estimate_text_tokens(profile: RequestProfile, rate: float | None = None) -> int:
    """อักขระที่ต้องเดา บวกกับ token ที่ไม่ต้องเดา

    `pretokenized_tokens` ไม่ใช่ค่าประมาณ: /v1/embeddings รับ token id ตรง ๆ ได้
    และคนที่ส่ง id มาก็บอกจำนวนที่แน่นอนมาแล้ว · ไม่บวกตรงนี้ = คำขอที่ส่ง token id
    ถูกคิดเป็น 0 token ทั้งที่ backend รันเต็ม ๆ ซึ่งคือช่องโหว่โควตาที่เปิดทิ้งไว้
    """
    wide = profile.text_wide_chars
    symbols = profile.text_symbol_chars
    # ที่เหลือคือตัวอักษรละตินกับช่องว่าง · ไม่ติดลบแม้โปรไฟล์ถูกสร้างขึ้นเองโดยไม่ได้แยกชนิด
    plain = max(0, profile.text_chars - wide - symbols)
    return (int(plain / CHARS_PER_TOKEN)
            + int(symbols / SYMBOL_CHARS_PER_TOKEN)
            + int(wide / wide_rate(rate))
            + profile.pretokenized_tokens)


def estimate_prompt_tokens(profile: RequestProfile, rate: float | None = None) -> int:
    return estimate_text_tokens(profile, rate) + estimate_visual_tokens(profile)


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


class OutputMeter:
    """นับสิ่งที่ backend เขียนออกมาและเกตเวย์ส่งต่อ — ใช้เมื่อ backend ไม่รายงาน usage

    เดิมไม่มีตัวนี้: `resolve_usage(profile, None)` ตอบ `output_tokens=0` ตายตัว ทั้งที่
    เกตเวย์เป็นคนส่งทุก delta ออกไปเองกับมือ · usage chunk เป็นสิ่ง *สุดท้าย* ที่ backend ส่ง
    อะไรก็ตามที่จบ stream ก่อนถึงตรงนั้นจึงได้ output ฟรีทั้งก้อน — ผู้เรียกกด Esc ตอนคำตอบ
    ใกล้จบ · backend หลุดกลางทาง · หรือ backend ที่ตอบ 200 โดยไม่มีบล็อก `usage` เลย
    (ตรวจพบ 2026-10-06: คำตอบ 2,000 อักขระ แถว usage บันทึก 0 estimated)

    เก็บ *จำนวนอักขระแยกชนิด* ไม่ใช่ตัวข้อความ และไม่ปัดเศษต่อชิ้น: delta หนึ่งชิ้นยาว 2–4
    อักขระ ถ้าประมาณทีละชิ้นแล้วปัดลง ทุกชิ้นจะเป็น 0 · ปัดครั้งเดียวตอนจบด้วยสูตรเดียวกับ
    ฝั่ง input (`estimate_text_tokens`) และอัตราของโมเดลเดียวกัน
    """

    __slots__ = ("letters", "symbols", "wide")

    def __init__(self) -> None:
        self.letters = 0
        self.symbols = 0
        self.wide = 0

    def add(self, text: object) -> None:
        if not isinstance(text, str) or not text:
            return
        if text.isascii():
            symbols = len(_ASCII_SYMBOL.findall(text))
            self.symbols += symbols
            self.letters += len(text) - symbols
            return
        plain = text.encode("ascii", "ignore").decode("ascii")
        symbols = len(_ASCII_SYMBOL.findall(plain))
        self.wide += len(text) - len(plain)
        self.symbols += symbols
        self.letters += len(plain) - symbols

    @property
    def chars(self) -> int:
        return self.letters + self.symbols + self.wide

    def tokens(self, rate: float | None = None) -> int:
        if not self.chars:
            return 0
        estimate = (int(self.letters / CHARS_PER_TOKEN)
                    + int(self.symbols / SYMBOL_CHARS_PER_TOKEN)
                    + int(self.wide / wide_rate(rate)))
        # ส่งเนื้อหาออกไปแล้ว = ไม่มีวันเป็น 0 · "ok" สองตัวอักษรปัดลงได้ 0 พอดี
        return max(estimate, 1)


def resolve_usage(profile: RequestProfile, upstream_usage: dict | None,
                  rate: float | None = None, *,
                  relayed: OutputMeter | None = None) -> TokenUsage:
    """Turn a backend usage object (or its absence) into the split we store.

    OpenAI-shaped backends report `prompt_tokens` / `completion_tokens`;
    Anthropic-shaped ones report `input_tokens` / `output_tokens`. Neither
    separates visual from text, so we attribute the estimated visual portion and
    treat the remainder as text.

    `relayed` คือสิ่งที่ส่งต่อไปแล้วจริง (ดู OutputMeter) · ใช้ก็ต่อเมื่อ backend ไม่ได้บอก
    จำนวน output มาเอง — ตัวเลขที่ backend วัดชนะค่าประมาณเสมอ · แถวที่ output มาจากการ
    ประมาณติดป้าย `estimated` แม้ input จะมาจาก backend เพราะป้ายนี้มีไว้บอกว่า "ในแถวนี้
    มีตัวเลขที่ไม่ได้วัด" ไม่ใช่ "ทุกตัวเป็นค่าประมาณ"
    """
    visual_estimate = estimate_visual_tokens(profile)
    relayed_tokens = relayed.tokens(rate) if relayed is not None else 0

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
            measured = completion > 0 or relayed_tokens == 0
            return TokenUsage(
                text_input_tokens=max(prompt - visual, 0),
                visual_input_tokens=visual,
                output_tokens=completion if measured else relayed_tokens,
                accounting="upstream" if measured else "estimated",
            )

    return TokenUsage(
        text_input_tokens=estimate_text_tokens(profile, rate),
        visual_input_tokens=visual_estimate,
        output_tokens=relayed_tokens,
        accounting="estimated",
    )


def resolve_pooling_usage(profile: RequestProfile, upstream_usage: dict | None,
                          rate: float | None = None) -> TokenUsage:
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
        text_input_tokens=estimate_text_tokens(profile, rate),
        visual_input_tokens=0,
        output_tokens=0,
        accounting="estimated",
    )
