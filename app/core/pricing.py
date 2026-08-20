"""ราคาต่อ token ของ API เชิงพาณิชย์ — ใช้ตอบว่า "ถ้าไม่ได้รันเอง จะจ่ายเท่าไร"

ทำไมเป็นตารางนิ่งในโค้ด ไม่ใช่ดึงสด:
โรงเรียนหลายแห่งอยู่หลัง proxy หรือไม่มีเน็ตออกนอกเลย · รายงานที่ต้องต่อเน็ตถึงจะขึ้น
คือรายงานที่ใช้ไม่ได้ในวันที่ต้องใช้ · ราคาขยับไม่บ่อยและตัวเลขนี้เป็น "ประมาณการเพื่อ
เปรียบเทียบ" อยู่แล้ว ไม่ใช่ใบแจ้งหนี้ — ความสด 100% จึงไม่คุ้มกับการพึ่งเน็ต

ตัวเลขเป็น USD ต่อ 1 token · อ้างอิงราคา list ที่ผู้ให้บริการประกาศไว้ ณ วันที่ระบุ
อัปเดตได้โดยแก้ที่นี่ที่เดียว แล้ว `updated` จะไปโผล่ในรายงานเอง เพื่อให้คนอ่านรู้ว่า
ตัวเลขเก่าแค่ไหน แทนที่จะเดา
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_UPDATED = "2026-08-20"


@dataclass(frozen=True)
class Price:
    label: str
    input_per_token: float
    output_per_token: float
    note: str = ""


# ราคา list ของผู้ให้บริการ (USD / 1M token หารด้วยล้านแล้ว)
BASELINES: dict[str, Price] = {
    "gpt-4o": Price("OpenAI GPT-4o", 2.50 / 1_000_000, 10.00 / 1_000_000),
    "gpt-4o-mini": Price("OpenAI GPT-4o mini", 0.15 / 1_000_000, 0.60 / 1_000_000),
    "claude-sonnet-4": Price("Anthropic Claude Sonnet 4", 3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-haiku-3-5": Price("Anthropic Claude Haiku 3.5", 0.80 / 1_000_000, 4.00 / 1_000_000),
    "gemini-2-5-pro": Price("Google Gemini 2.5 Pro", 1.25 / 1_000_000, 10.00 / 1_000_000),
    "gemini-2-5-flash": Price("Google Gemini 2.5 Flash", 0.30 / 1_000_000, 2.50 / 1_000_000),
}

DEFAULT_BASELINE = "gpt-4o-mini"


def estimate(baseline: str, input_tokens: int, output_tokens: int) -> float:
    """ค่าใช้จ่ายโดยประมาณถ้าทราฟฟิกชุดนี้วิ่งผ่าน baseline ที่เลือก (USD)"""
    price = BASELINES.get(baseline) or BASELINES[DEFAULT_BASELINE]
    return input_tokens * price.input_per_token + output_tokens * price.output_per_token


def catalogue() -> list[dict]:
    """รายการ baseline ให้หน้าเว็บทำ dropdown — ไม่ต้อง hardcode ซ้ำฝั่ง client"""
    return [
        {
            "id": key,
            "label": price.label,
            "input_per_1m": round(price.input_per_token * 1_000_000, 4),
            "output_per_1m": round(price.output_per_token * 1_000_000, 4),
        }
        for key, price in BASELINES.items()
    ]
