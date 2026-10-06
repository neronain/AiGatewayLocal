"""แค็ตตาล็อกโมเดลในรูปที่ Codex อ่านออก

Codex ไม่ได้อ่าน `/v1/models` แบบ OpenAI SDK · ตัวจัดการโมเดลของมัน
(`codex-rs/models-manager`) ยิง `GET <base_url>/models?client_version=<x.y.z>` แล้ว
decode เป็น `{"models": [ModelInfo, ...]}` · รูป `{"object":"list","data":[...]}` ไม่มี
ฟิลด์ `models` จึง decode ไม่ผ่าน มันพิมพ์ error ทุกรอบ แล้วใช้ metadata สำรองกับ
**ทุก alias**: หน้าต่าง 272,000 token และรับรูปได้ ไม่ว่าโมเดลจริงจะเป็นอย่างไร
(เจอจริง 2026-10-06 · Codex CLI 0.149.1)

ผลของตัวเลขผิดไม่ใช่แค่ข้อความเตือน: Codex ย่อบทสนทนาเมื่อถึง 90% ของหน้าต่างที่มัน
*เชื่อ* · โมเดล 131K ที่ถูกเชื่อว่าเป็น 272K จะไม่ถูกย่อเลย จนเกตเวย์ปฏิเสธด้วย
CONTEXT_LENGTH_EXCEEDED กลางงาน

ค่าในแต่ละรายการมีที่มาสองแบบ และจงใจไม่ปนกัน:

* **ของโมเดล** — มาจากทะเบียนเท่านั้น: หน้าต่าง context, modality ขาเข้า, ชื่อ, คำอธิบาย
* **ของตัว Codex เอง** (เครื่องมือ shell, การตัด output ของ tool, system prompt) — ทะเบียน
  ไม่รู้เรื่องพวกนี้ จึงใช้ค่าเดียวกับ metadata สำรองของ Codex (`model_info_from_slug`)
  ซึ่งคือสิ่งที่ทุก alias ได้อยู่แล้วก่อนหน้านี้ · ไม่มีอะไรเปลี่ยนนอกจากสิ่งที่เรารู้จริง

ไม่มี `max_output_tokens`: `ModelInfo` ไม่มีช่องให้ใส่ และ Codex ไม่ส่งค่านี้มากับคำขอ ·
เพดาน output บังคับที่เกตเวย์อยู่แล้ว
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.registry.schema import Modality, ModelDefinition, Purpose

_PROMPT = Path(__file__).resolve().parent.parent / "vendor" / "codex" / "prompt.md"


@lru_cache(maxsize=1)
def base_instructions() -> str:
    """system prompt ที่ Codex ใช้กับโมเดลที่มันไม่รู้จัก — ดู app/vendor/codex/README.md

    ต้องส่งไปด้วยทุกรายการ: รายการเดียวที่ไม่มี instructions ทำให้ Codex **decode ไม่ผ่าน
    ทั้งคำตอบ** ("is missing both `base_instructions` and ...") ไม่ใช่กลับไปใช้ค่าตั้งต้น
    ของมันเฉพาะตัวนั้น
    """
    return _PROMPT.read_text(encoding="utf-8")


def usable_from_codex(model: ModelDefinition) -> bool:
    """Codex เรียก alias นี้ได้จริงไหม

    Codex พูด Responses API อย่างเดียว สตรีมเสมอ และแนบ tools มาทุกคำขอ · ขาดข้อไหน
    เกตเวย์ปฏิเสธด้วย 400 · เสนอโมเดลที่จะถูกปฏิเสธแย่กว่าไม่เสนอ: มันขึ้นในตัวเลือก
    คนเลือก แล้ว error มาถึงหลังพิมพ์ prompt เสร็จ
    """
    caps = model.spec.capabilities
    return bool(
        model.spec.protocols.responses and caps.chat and caps.tools and caps.streaming
    )


def _input_modalities(model: ModelDefinition) -> list[str]:
    # เงื่อนไขเดียวกับด่านใน capability.validate_model_capabilities — บอก Codex ว่ารับรูป
    # ทั้งที่ด่านจะปฏิเสธ คือให้มันแนบรูปไปชน 400
    #
    # มีแค่ text กับ image: enum ของ Codex ไม่มี video และค่าที่มันไม่รู้จัก **ทำให้ decode
    # ล้มทั้งคำตอบ** ไม่ใช่แค่รายการเดียว · audio มีใน enum แต่ตัวแปล /v1/responses
    # ยังไม่รับเสียงขาเข้า
    modalities = ["text"]
    if model.spec.capabilities.vision and Modality.IMAGE in model.spec.modalities.input:
        modalities.append("image")
    return modalities


def _entry(model: ModelDefinition, priority: int) -> dict[str, Any]:
    context = model.spec.limits.context_tokens
    return {
        "slug": model.alias,
        "display_name": model.metadata.display_name,
        "description": model.metadata.description or None,
        # ── ของโมเดล ──
        "context_window": context,
        # เพดานของ `model_context_window` ใน config ฝั่งผู้ใช้ — ลดได้ ตั้งเกินของจริงไม่ได้
        "max_context_window": context,
        "input_modalities": _input_modalities(model),
        # ตัวแปล /v1/responses ไม่ส่ง reasoning.effort ต่อให้ backend · เสนอระดับให้เลือก
        # คือเสนอปุ่มที่กดแล้วไม่มีอะไรเกิดขึ้น
        "supported_reasoning_levels": [],
        # ── ของตัว Codex: ค่าเดียวกับ metadata สำรองของมัน ──
        "shell_type": "default",
        "apply_patch_tool_type": None,
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
        "support_verbosity": False,
        "default_verbosity": None,
        "supports_image_detail_original": False,
        "experimental_supported_tools": [],
        "base_instructions": base_instructions(),
        # ── การแสดงในตัวเลือกโมเดล ──
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "availability_nux": None,
        "upgrade": None,
    }


def codex_models(models: Iterable[ModelDefinition]) -> list[dict[str, Any]]:
    """รายการ `models` สำหรับ Codex จากโมเดลที่ผู้เรียก *มีสิทธิ์* อยู่แล้ว

    ผู้เรียกกรองสิทธิ์มาก่อน · ที่นี่คัดต่อแค่ว่า Codex ใช้ได้ไหม · โมเดลงานโค้ดขึ้นก่อน
    เพราะ Codex หยิบตัวแรกเป็นค่าตั้งต้นเมื่อผู้ใช้ไม่ได้ระบุโมเดล
    """
    usable = sorted(
        (m for m in models if usable_from_codex(m)),
        key=lambda m: (Purpose.CODING not in m.spec.purpose, m.alias),
    )
    return [_entry(model, priority) for priority, model in enumerate(usable)]
