"""`model="auto"` — ให้เกตเวย์เลือกโมเดลเองจากสิ่งที่คำขอต้องการ

แนวคิดมาจาก OrcaRouter-Lite แต่ **แกนที่ใช้จัดอันดับต่างกันคนละเรื่อง** · ของเขา proxy
ไปหา API ที่คิดเงินต่อ token จึงเรียงตาม "ถูกที่สุดที่ทำงานนั้นได้" · ของเราโมเดลรันบน
เครื่องที่โรงเรียนซื้อมาเอง เงินไม่ใช่ตัวแปรต่อคำขอ — สิ่งที่หายากคือ **เวลา** เราจึงเรียง
ตามความเร็วที่วัดได้จริงจากทราฟฟิกของเกตเวย์เอง

สิ่งที่ตัดสินใจ *ไม่* ทำ:

* **ไม่ใช้ `auto` ข้ามสิทธิ์** — ผู้เลือกคือเกตเวย์ แต่ตัวเลือกมีแค่โมเดลที่สมาชิกคนนั้น
  ใช้ได้อยู่แล้ว · ไม่งั้น `auto` จะกลายเป็นช่องทางเข้าถึงโมเดลที่แอดมินกันไว้
* **ไม่ทิ้งโมเดลที่ยังไม่มีสถิติ** — เกตเวย์ที่เพิ่งตั้งยังไม่มีข้อมูลสักตัว ถ้าตัดทิ้ง
  `auto` จะใช้ไม่ได้เลยในวันแรก · ตัวที่ยังไม่มีข้อมูลไปอยู่ท้ายแถว ไม่ใช่หายไป
  (ต่างจาก OrcaRouter ที่ตัดทิ้งได้เพราะแค็ตตาล็อกเขามีเป็นร้อยตัว)
* **ไม่เดาว่าผู้ใช้ต้องการอะไรจากเนื้อความ** — ดูแค่รูปร่างของคำขอ (มีภาพไหม ขอ tool
  ไหม ยาวแค่ไหน) เหมือนที่ `app/core/rules.py` ทำอยู่ ด้วยเหตุผลเดียวกัน

แกนคุณภาพ (2026-10-09)
----------------------
เรียงตามความเร็วอย่างเดียวมีผลที่ตามมาแน่นอน: ฟลีตที่มีตัวเล็ก (เร็ว) กับตัวใหญ่ (เก่งกว่า) ให้
คนคนเดียวกันใช้ได้ทั้งคู่ `auto` จะส่งงานไปตัวเล็กเสมอ · จึงเพิ่มกลยุทธ์อีกสองแบบ **ที่ผู้ดูแลต้อง
เลือกเอง** — ค่าตั้งต้นยังเป็น `fastest` และข้อตัดสินใจสามข้อข้างบนยังอยู่ครบทุกกลยุทธ์:

* `quality`   คะแนนที่ผู้ดูแลตั้ง (`spec.quality_score`) สูงสุดก่อน · เท่ากันให้ตัวที่เร็วกว่า
* `balanced`  ชั่งคุณภาพกับความเร็ว 2:1 (ดู `QUALITY_WEIGHT`)

ต่างจาก OrcaRouter-Lite สามจุด ทุกจุดมาจากการที่ฟลีตเรามีไม่กี่ตัว:

* **คะแนนมาจากผู้ดูแลเท่านั้น** ไม่มีดัชนีภายนอก — โมเดลเราเป็น fine-tune/quantise ที่ไม่มีใครจัด
  อันดับให้ และเกตเวย์ต้องทำงานได้โดยไม่มีเน็ต
* **ตัวที่ไม่มีคะแนนไปท้ายแถว ไม่ถูกตัดทิ้ง** — กติกาเดียวกับตัวที่ไม่มีสถิติความเร็ว
* **ไม่ใช้ min-max** — กับผู้สมัครสองตัว min-max ให้ 0 กับ 1 เสมอไม่ว่าจะต่างกัน 2% หรือ 5 เท่า
  ขนาดของความต่างหายหมด แล้ว `balanced` จะเท่ากับ "แกนที่น้ำหนักมากกว่าชนะทุกครั้ง" · เราเทียบ
  คะแนนตามสเกล 0–100 ที่ผู้ดูแลตั้งตรง ๆ และเทียบความเร็วเป็นสัดส่วนของตัวที่เร็วที่สุด
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from app.core.capability import endpoint_supports
from app.core.multimodal import RequestProfile
from app.core.perf import ModelPerf, PerfStore
from app.core.rules import can_serve
from app.core.tokens import estimate_prompt_tokens, rates_of
from app.db.models import AUTO_STRATEGY_KEY, GatewaySetting
from app.registry.schema import ModelDefinition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ALIAS = "auto"

# กลยุทธ์ที่ผู้ดูแลเลือกได้ · เก็บที่ gateway_settings (ดู `configured_strategy`) ·
# ค่าที่ไม่รู้จัก — เช่นที่เวอร์ชันใหม่กว่าเขียนไว้แล้วถอยเวอร์ชันกลับมา — ถือเป็นค่าตั้งต้น
STRATEGIES = ("fastest", "roomiest", "quality", "balanced")
DEFAULT_STRATEGY = "fastest"

# ── น้ำหนักของ `balanced`: คุณภาพ 2 ส่วน ความเร็ว 1 ส่วน ──
#
#   คะแนนรวม (0–100) = (2 × quality_score + 100 × ความเร็วเทียบตัวเร็วสุดในกลุ่ม) ÷ 3
#
# อ่านเป็นกติกาประโยคเดียวที่ผู้ดูแลใช้ตอนให้คะแนนได้: **คุณภาพที่มากกว่า 1 คะแนน คุ้มกับความเร็วที่
# เสียไป 2% ของตัวเร็วสุด** — ตัวที่ดีกว่า 20 คะแนนยอมให้ช้ากว่าได้ถึง 40% · ช้ากว่านั้นตัวเร็วชนะ
#
# ทำไมไม่ 1:1 — สองแกนกว้างไม่เท่ากันโดยธรรมชาติ · ตัวเล็กกับตัวใหญ่บนเครื่องเดียวกันเร็วต่างกัน
# 3–5 เท่าเป็นเรื่องปกติ = กินแกนความเร็วไป 67–80% ส่วนคะแนนของโมเดลที่มีคนยอมเอาขึ้นฟลีต
# มักห่างกันไม่ถึงครึ่งสเกล · ชั่งเท่ากันแล้วแกนความเร็วตัดสินแทบทุกครั้ง `balanced` จะให้ผลเดียวกับ
# `fastest` ในเคสที่มันมีไว้แก้พอดี (คะแนน 90 ที่ 40 tok/s แพ้คะแนน 40 ที่ 200 tok/s: 55 ต่อ 70)
#
# ช่วง 3–5 เท่ากับ "ไม่ถึงครึ่งสเกล" เป็นการประมาณ ยังไม่ได้วัดจากทราฟฟิกจริงของฟลีตไหน —
# ถ้าฟลีตไม่เป็นแบบนั้น ตัวเลขที่ควรแก้คือสองบรรทัดนี้ ไม่ใช่สูตร
QUALITY_WEIGHT = 2 / 3
SPEED_WEIGHT = 1 / 3

# กันคำขอที่พอดีเป๊ะจนไม่เหลือที่ให้คำตอบ — เท่ากับ CONTEXT_TOLERANCE ใน rules.py
_HEADROOM_TOKENS = 512


@dataclass(frozen=True)
class Candidate:
    """ตัวเลขของผู้สมัครหนึ่งตัว **ณ ตอนที่จัดอันดับ** — ชุดเดียวกับที่ใช้เรียง และชุดเดียวกับที่พรีวิวโชว์

    เก็บเป็นสำเนา ไม่ชี้ไปที่ `ModelPerf` ตัวจริงซึ่งคำขอที่จบทีหลังแก้ค่าอยู่ตลอด — ไม่งั้นหน้าพรีวิว
    จะอ่านได้ตัวเลขคนละชุดกับที่การตัดสินใจเพิ่งใช้ (แนวเดียวกับ `ScoringDetail` ของ OrcaRouter-Lite)
    """

    model: ModelDefinition
    quality: int | None            # คะแนนที่ผู้ดูแลตั้ง · None = ยังไม่ได้ตั้ง
    perf: ModelPerf | None         # สำเนา · None = ยังไม่เคยเห็นคำขอของตัวนี้เลย
    # สามช่องนี้มีค่าเฉพาะกลยุทธ์ `balanced`
    speed: float | None = None     # 0–1 เทียบกับตัวเร็วสุดที่วัดได้ในกลุ่ม
    speed_assumed: bool = False    # True = ยังไม่มีสถิติ จึงคิดเสมือนเร็วเท่าตัวเร็วสุด
    combined: float | None = None  # 0–100 · None = ไม่มีคะแนนคุณภาพให้ชั่ง

    @property
    def output_tps(self) -> float | None:
        """None เมื่อสถิติยังไม่ถึงเกณฑ์ (`MIN_SAMPLES`) — ตัวจัดอันดับไม่เชื่อตัวเลขก่อนถึงตอนนั้น"""
        return self.perf.output_tps if self.perf and self.perf.usable else None

    @property
    def ttft_ms(self) -> float | None:
        return self.perf.ttft_ms if self.perf and self.perf.usable else None

    @property
    def samples(self) -> int:
        return self.perf.samples if self.perf else 0


@dataclass(frozen=True)
class AutoChoice:
    """โมเดลที่เลือก + เหตุผล — เหตุผลไปโผล่ใน log และ header ให้ไล่ปัญหาได้"""

    model: ModelDefinition
    reason: str
    ranked: tuple[str, ...]
    strategy: str = DEFAULT_STRATEGY
    # เรียงตามอันดับ ตรงกับ `ranked` ตัวต่อตัว
    candidates: tuple[Candidate, ...] = ()

    @property
    def fallbacks(self) -> tuple[str, ...]:
        return self.ranked[1:]


# surface ที่เกตเวย์แปลเป็น chat completions ให้ได้ — เครื่องที่พูดแต่ openai จึงเสิร์ฟได้ด้วย
# (ตรงกับ `_select` ใน app/api/anthropic.py และ app/api/responses.py)
_TRANSLATED_SURFACES = frozenset({"anthropic", "responses"})


async def configured_strategy(session: AsyncSession) -> str:
    """กลยุทธ์ที่ผู้ดูแลเลือกไว้ — อ่านจากฐานข้อมูลทุกครั้ง ไม่ cache ใน process

    เกตเวย์รันหลาย worker: ค่าที่จำไว้ในหน่วยความจำของตัวที่รับคำสั่งเปลี่ยน จะมีผลกับคำขอแค่ส่วนที่
    บังเอิญตกมาที่ตัวนั้น · ราคาคือ SELECT ด้วย primary key หนึ่งครั้ง เฉพาะคำขอที่ส่ง
    `model="auto"` ซึ่งเปิด session อ่านสิทธิ์อยู่แล้วในจังหวะเดียวกัน — **การจัดอันดับเอง** ยังไม่แตะ
    ฐานข้อมูลตามที่ app/core/perf.py ตั้งใจไว้
    """
    row = await session.get(GatewaySetting, AUTO_STRATEGY_KEY)
    value = row.value if row is not None else ""
    if value and value not in STRATEGIES:
        log.warning("auto: ไม่รู้จักกลยุทธ์ %r ที่ตั้งไว้ — ใช้ %s แทน", value, DEFAULT_STRATEGY)
    return value if value in STRATEGIES else DEFAULT_STRATEGY


def _serves(model: ModelDefinition, profile: RequestProfile, protocol: str) -> bool:
    """มีเครื่องของโมเดลนี้อย่างน้อยหนึ่งเครื่องที่รับคำขอรูปนี้ได้ไหม (ด่านที่สองของ capability)"""
    wire = (protocol, "openai") if protocol in _TRANSLATED_SURFACES else (protocol,)
    return any(
        endpoint_supports(e, profile, p) for e in model.spec.endpoints for p in wire
    )


def _fits(model: ModelDefinition, profile: RequestProfile) -> bool:
    """นับด้วยอัตรา tokenizer ของโมเดลตัวนั้น — ตัวเลขเดียวกับที่ด่าน context จะใช้ตัดสิน

    เดิมผู้เรียกประมาณครั้งเดียวด้วยค่าสำรองกลาง (1.6) แล้วส่งตัวเลขเดียวมาเทียบกับทุกโมเดล ·
    ภาษาไทยต่างกันได้ 2.1 เท่าระหว่างโมเดล: ตัวที่ tokenizer ดีถูกตัดทิ้งทั้งที่รับได้ และตัวที่
    tokenizer ฉีกถูกเลือกทั้งที่ด่านถัดไปจะปฏิเสธ — `auto` จึงตอบ 400 กับคำขอที่มีโมเดลรับไหว
    """
    limit = model.spec.limits.context_tokens or 0
    prompt_tokens = estimate_prompt_tokens(profile, rates_of(model.spec))
    return not limit or prompt_tokens + _HEADROOM_TOKENS <= limit


def candidates(
    models: list[ModelDefinition],
    *,
    profile: RequestProfile,
    protocol: str,
) -> list[ModelDefinition]:
    """โมเดลที่รับคำขอรูปนี้ได้จริง — กรองด้วยข้อเท็จจริง ไม่ใช่ความชอบ

    **ต้องกรองด้วยด่านชุดเดียวกับที่ทางเดินคำขอจะตรวจต่อ** (`rules.can_serve`: โมเดลเปิดอยู่ ·
    เปิด surface นี้ · ประกาศ capability ที่คำขอต้องใช้) ไม่ใช่แค่ดูเครื่องกับขนาด context ·
    เดิมดูแค่สองอย่างหลัง แล้วด่าน capability กับ surface ไปตรวจทีหลังกับ *ตัวที่ชนะตัวเดียว*:
    ตั้ง `coding` เป็น `tools: false` แล้วขอ `auto` พร้อม tools ได้ 400
    MODEL_CAPABILITY_NOT_SUPPORTED ทั้งที่ gemma-vision กับ muse-local รับได้ · ปิด
    `protocols.openai` ของ `coding` ได้ 400 PROTOCOL_NOT_SUPPORTED ทั้งที่อีกสองตัวเสิร์ฟ chat
    อยู่ (ตรวจ 2026-10-06) — `auto` ที่เลือกตัวซึ่งด่านถัดไปปฏิเสธ แย่กว่าไม่มี `auto`
    """
    return [
        m for m in models
        if m.alias != ALIAS
        and can_serve(m, profile, protocol)
        and _serves(m, profile, protocol)
        and _fits(m, profile)
    ]


def _speed_key(perf: ModelPerf | None) -> tuple[int, float, float]:
    """เรียงเร็วก่อน · ตัวที่ยังไม่มีข้อมูลพอไปท้ายแถว ไม่ใช่ถูกตัดทิ้ง

    คีย์แรกเป็น 0/1 เพื่อแยกกลุ่ม "มีข้อมูล" ออกจาก "ยังไม่มี" ก่อนเทียบตัวเลข —
    ไม่งั้นค่า None ต้องถูกแทนด้วยตัวเลขสมมติ ซึ่งจะกลายเป็นการเดาว่ามันเร็วหรือช้า
    """
    if perf is None or not perf.usable:
        return (1, 0.0, 0.0)
    return (0, -(perf.output_tps or 0.0), perf.ttft_ms or 0.0)


def _quality_key(candidate: Candidate) -> tuple:
    """คะแนนสูงก่อน · เท่ากันให้ตัวที่เร็วกว่า · ตัวที่ไม่มีคะแนนไปท้ายแถว เรียงกันเองตามความเร็ว

    ไม่แทน None ด้วยตัวเลข เหตุผลเดียวกับ `_speed_key`: ใส่ 0 = ตัดสินแทนผู้ดูแลว่าตัวนี้แย่ที่สุด ·
    ผลที่ตามมาและตั้งใจ: ยังไม่มีตัวไหนมีคะแนนเลย = ลำดับเดียวกับ `fastest` ทุกตำแหน่ง
    """
    if candidate.quality is None:
        return (1, 0, *_speed_key(candidate.perf))
    return (0, -candidate.quality, *_speed_key(candidate.perf))


def _balanced_key(candidate: Candidate) -> tuple:
    """คะแนนรวมสูงก่อน · เสมอกันให้ตัวที่เร็วกว่า · ตัวที่ไม่มีคะแนนคุณภาพไปท้ายแถวเหมือน `quality`"""
    if candidate.combined is None:
        return (1, 0.0, *_speed_key(candidate.perf))
    return (0, -candidate.combined, *_speed_key(candidate.perf))


def _weigh(pool: list[Candidate]) -> list[Candidate]:
    """เติมตัวเลขของ `balanced` ให้ผู้สมัครทุกตัว — สองกรณีที่ข้อมูลขาดถูกเลือกทางไว้แล้ว ไม่ได้ปล่อยตามบุญตามกรรม

    **ไม่มีคะแนนคุณภาพ** → ไม่มีคะแนนรวม ไปท้ายแถว · คะแนนไม่ได้มาเองตามทราฟฟิก ไม่มีคนตั้งก็ไม่มีวัน
    มี การใส่ตัวเลขแทนคือเดาใจผู้ดูแล · ผลข้างเคียงที่ตั้งใจ: โมเดลที่เพิ่งเพิ่มเข้าทะเบียนไม่แย่งงาน
    `auto` จนกว่าจะมีคนให้คะแนน

    **มีคะแนนแต่ยังไม่มีสถิติความเร็ว** → คิดเสมือนเร็วเท่าตัวที่เร็วที่สุด (`speed_assumed`) ·
    ต่างจาก `fastest` ที่ส่งไปท้ายแถว และต่างโดยตั้งใจ: สถิติอยู่ในหน่วยความจำของแต่ละ worker และ
    หายทุกครั้งที่รีสตาร์ต ถ้า "ไม่มีสถิติ" แปลว่าแพ้ ตัวที่บังเอิญถูกวัดก่อนจะชนะตลอดไป เพราะตัวที่
    แพ้ไม่ถูกเลือกจึงไม่มีวันถูกวัด — ผลของ `balanced` จะขึ้นกับว่าใครถูกเรียกก่อนหลังรีสตาร์ต ·
    ให้เต็มไว้ก่อนแล้ว: ถ้าเต็มแล้วยังแพ้ก็ไม่ต้องวัด · ถ้าชนะก็ถูกเลือก ถูกวัดภายใน `MIN_SAMPLES`
    คำขอ แล้วตัวเลขจริงมาแทน · ยังไม่มีใครมีสถิติเลย (เพิ่งรีสตาร์ต) = เรียงตามคุณภาพล้วน ๆ

    ปัดคะแนนรวมเหลือทศนิยมหนึ่งตำแหน่ง **ก่อน** ใช้เรียง: ตัวเลขที่โชว์คือตัวเลขที่ตัดสิน และคู่ที่ห่างกัน
    น้อยกว่านั้น (ไม่ถึง 0.3% ของความเร็ว) ถือว่าเสมอ ให้ตัวที่เร็วกว่า — ไม่ปล่อยให้เศษทศนิยมของ float
    เป็นคนเลือก
    """
    fastest = max((c.output_tps for c in pool if c.output_tps), default=None)
    weighed = []
    for candidate in pool:
        tps = candidate.output_tps
        if fastest and tps:
            speed, assumed = round(tps / fastest, 3), False
        elif candidate.quality is not None:
            speed, assumed = 1.0, True
        else:
            speed, assumed = None, False
        combined = None
        if candidate.quality is not None and speed is not None:
            combined = round(
                QUALITY_WEIGHT * candidate.quality + SPEED_WEIGHT * 100 * speed, 1)
        weighed.append(replace(candidate, speed=speed, speed_assumed=assumed, combined=combined))
    return weighed


def _rank(pool: list[ModelDefinition], perf: PerfStore, strategy: str) -> list[Candidate]:
    """เรียงผู้สมัครตามกลยุทธ์ · sort ของ Python คงลำดับเดิมเมื่อคีย์เท่ากัน = ลำดับในทะเบียน"""
    rows = []
    for model in pool:
        stats = perf.get(model.alias)
        rows.append(Candidate(model, model.spec.quality_score, replace(stats) if stats else None))

    if strategy == "roomiest":
        rows.sort(key=lambda c: -(c.model.spec.limits.context_tokens or 0))
    elif strategy == "quality":
        rows.sort(key=_quality_key)
    elif strategy == "balanced":
        rows = _weigh(rows)
        rows.sort(key=_balanced_key)
    else:
        rows.sort(key=lambda c: _speed_key(c.perf))
    return rows


def _speed_reason(top: Candidate) -> str:
    return (
        f"เร็วที่สุดที่วัดได้ ({top.output_tps:.0f} tok/s)"
        if top.output_tps else "ยังไม่มีสถิติความเร็ว — เลือกตัวแรกที่รับคำขอนี้ได้"
    )


def _reason(top: Candidate, strategy: str) -> str:
    if strategy == "roomiest":
        return "context เหลือมากที่สุด"
    if strategy not in ("quality", "balanced"):
        return _speed_reason(top)
    if top.quality is None:
        # ตัวที่มีคะแนนอยู่หน้าตัวที่ไม่มีเสมอ — อันดับหนึ่งไม่มีคะแนน = ไม่มีผู้สมัครตัวไหนมี
        return f"ยังไม่มีโมเดลที่รับคำขอนี้ได้ตัวไหนถูกให้คะแนนคุณภาพ · {_speed_reason(top)}"
    if strategy == "quality":
        return f"คะแนนคุณภาพสูงสุด ({top.quality}/100)"
    speed = (
        "ยังไม่มีสถิติความเร็ว คิดเสมือนเร็วเท่าตัวเร็วสุด" if top.speed_assumed
        else f"ความเร็ว {top.speed:.0%} ของตัวเร็วสุด"
    )
    return f"คะแนนรวมสูงสุด {top.combined:.1f} (คุณภาพ {top.quality}/100 · {speed})"


def choose(
    models: list[ModelDefinition],
    *,
    profile: RequestProfile,
    protocol: str,
    perf: PerfStore,
    strategy: str = DEFAULT_STRATEGY,
) -> AutoChoice | None:
    """เลือกโมเดลให้คำขอนี้ · คืน None เมื่อไม่มีตัวไหนรับได้เลย

    `models` ต้องถูกกรองสิทธิ์มาแล้วโดยผู้เรียก — โมดูลนี้ไม่รู้จักสมาชิกและไม่ควรรู้ ·
    กรองด้วยข้อเท็จจริง (`candidates`) เสร็จก่อนเสมอ แล้วค่อยเรียงตามกลยุทธ์ — คะแนนสูงแค่ไหน
    ก็ไม่ทำให้ตัวที่รับคำขอนี้ไม่ได้ถูกเลือก
    """
    pool = candidates(models, profile=profile, protocol=protocol)
    if not pool:
        return None
    if strategy not in STRATEGIES:
        strategy = DEFAULT_STRATEGY

    ranked = _rank(pool, perf, strategy)
    return AutoChoice(
        ranked[0].model, _reason(ranked[0], strategy),
        tuple(c.model.alias for c in ranked), strategy, tuple(ranked),
    )


def explain(choice: AutoChoice | None) -> list[dict]:
    """อันดับพร้อมตัวเลขที่ใช้ตัดสิน — ให้หน้าเว็บอธิบายได้ว่าทำไมถึงได้ตัวนี้

    รับ *ผลของ `choose`* ไม่ได้จัดอันดับเองอีกรอบ: เดิมเรียก `choose` แล้วกลับไปอ่านสถิติใหม่ทีละตัว
    ซึ่งคำขอที่จบระหว่างสองจังหวะนั้นเปลี่ยนค่าได้ — คำอธิบายกับของจริงเพี้ยนจากกันทั้งที่ใช้ตัวจัด
    อันดับตัวเดียวกัน · ตอนนี้ทุกตัวเลขมาจาก `Candidate` ชุดที่ถูกเรียงจริง
    """
    if choice is None:
        return []
    return [
        {
            "rank": rank,
            "alias": c.model.alias,
            "context_tokens": c.model.spec.limits.context_tokens or 0,
            "quality_score": c.quality,
            "output_tps": round(c.output_tps, 1) if c.output_tps else None,
            "ttft_ms": round(c.ttft_ms) if c.ttft_ms else None,
            "samples": c.samples,
            "speed": c.speed,
            "speed_assumed": c.speed_assumed,
            "combined": c.combined,
        }
        for rank, c in enumerate(choice.candidates, start=1)
    ]
