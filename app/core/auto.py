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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.capability import endpoint_supports
from app.core.multimodal import RequestProfile
from app.core.perf import ModelPerf, PerfStore
from app.core.tokens import estimate_prompt_tokens  # noqa: F401  (ผู้เรียกส่งค่ามาให้)
from app.registry.schema import ModelDefinition

log = logging.getLogger(__name__)

ALIAS = "auto"

# กันคำขอที่พอดีเป๊ะจนไม่เหลือที่ให้คำตอบ — เท่ากับ CONTEXT_TOLERANCE ใน rules.py
_HEADROOM_TOKENS = 512


@dataclass(frozen=True)
class AutoChoice:
    """โมเดลที่เลือก + เหตุผล — เหตุผลไปโผล่ใน log และ header ให้ไล่ปัญหาได้"""

    model: ModelDefinition
    reason: str
    ranked: tuple[str, ...]

    @property
    def fallbacks(self) -> tuple[str, ...]:
        return self.ranked[1:]


def _serves(model: ModelDefinition, profile: RequestProfile, protocol: str) -> bool:
    return any(endpoint_supports(e, profile, protocol) for e in model.spec.endpoints)


def _fits(model: ModelDefinition, prompt_tokens: int) -> bool:
    limit = model.spec.limits.context_tokens or 0
    return not limit or prompt_tokens + _HEADROOM_TOKENS <= limit


def candidates(
    models: list[ModelDefinition],
    *,
    profile: RequestProfile,
    protocol: str,
    prompt_tokens: int,
) -> list[ModelDefinition]:
    """โมเดลที่รับคำขอรูปนี้ได้จริง — กรองด้วยข้อเท็จจริง ไม่ใช่ความชอบ"""
    return [
        m for m in models
        if m.alias != ALIAS and _serves(m, profile, protocol) and _fits(m, prompt_tokens)
    ]


def _speed_key(perf: ModelPerf | None) -> tuple[int, float, float]:
    """เรียงเร็วก่อน · ตัวที่ยังไม่มีข้อมูลพอไปท้ายแถว ไม่ใช่ถูกตัดทิ้ง

    คีย์แรกเป็น 0/1 เพื่อแยกกลุ่ม "มีข้อมูล" ออกจาก "ยังไม่มี" ก่อนเทียบตัวเลข —
    ไม่งั้นค่า None ต้องถูกแทนด้วยตัวเลขสมมติ ซึ่งจะกลายเป็นการเดาว่ามันเร็วหรือช้า
    """
    if perf is None or not perf.usable:
        return (1, 0.0, 0.0)
    return (0, -(perf.output_tps or 0.0), perf.ttft_ms or 0.0)


def choose(
    models: list[ModelDefinition],
    *,
    profile: RequestProfile,
    protocol: str,
    prompt_tokens: int,
    perf: PerfStore,
    strategy: str = "fastest",
) -> AutoChoice | None:
    """เลือกโมเดลให้คำขอนี้ · คืน None เมื่อไม่มีตัวไหนรับได้เลย

    `models` ต้องถูกกรองสิทธิ์มาแล้วโดยผู้เรียก — โมดูลนี้ไม่รู้จักสมาชิกและไม่ควรรู้
    """
    pool = candidates(models, profile=profile, protocol=protocol, prompt_tokens=prompt_tokens)
    if not pool:
        return None

    if strategy == "roomiest":
        pool.sort(key=lambda m: -(m.spec.limits.context_tokens or 0))
        reason = "context เหลือมากที่สุด"
    else:
        pool.sort(key=lambda m: _speed_key(perf.get(m.alias)))
        top = perf.get(pool[0].alias)
        reason = (
            f"เร็วที่สุดที่วัดได้ ({top.output_tps:.0f} tok/s)"
            if top and top.usable else "ยังไม่มีสถิติความเร็ว — เลือกตัวแรกที่รับคำขอนี้ได้"
        )
    return AutoChoice(pool[0], reason, tuple(m.alias for m in pool))


def explain(
    models: list[ModelDefinition],
    *,
    profile: RequestProfile,
    protocol: str,
    prompt_tokens: int,
    perf: PerfStore,
    strategy: str = "fastest",
) -> list[dict]:
    """อันดับพร้อมตัวเลขที่ใช้ตัดสิน — ให้หน้าเว็บอธิบายได้ว่าทำไมถึงได้ตัวนี้

    ใช้ตัวจัดอันดับตัวเดียวกับ `choose` เพื่อไม่ให้คำอธิบายกับของจริงเพี้ยนจากกัน
    """
    choice = choose(models, profile=profile, protocol=protocol,
                    prompt_tokens=prompt_tokens, perf=perf, strategy=strategy)
    if choice is None:
        return []
    by_alias = {m.alias: m for m in models}
    rows = []
    for rank, alias in enumerate(choice.ranked, start=1):
        model = by_alias[alias]
        stats = perf.get(alias)
        rows.append({
            "rank": rank,
            "alias": alias,
            "context_tokens": model.spec.limits.context_tokens or 0,
            "output_tps": round(stats.output_tps, 1) if stats and stats.usable else None,
            "ttft_ms": round(stats.ttft_ms) if stats and stats.usable and stats.ttft_ms else None,
            "samples": stats.samples if stats else 0,
        })
    return rows
