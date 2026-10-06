"""ความคิดของโมเดล reasoning จาก backend ที่พูด chat completions

ตัวแปลทุกตัว (Anthropic · Responses) อ่านผ่านที่นี่ที่เดียว — ชื่อฟิลด์เปลี่ยนมาแล้วครั้งหนึ่ง
และตัวแปลที่อ่านชื่อเดียวคือตัวที่ทำความคิดหายเงียบ ๆ สำหรับครึ่งหนึ่งของฟลีต
"""

from __future__ import annotations

from typing import Any

__all__ = ["reasoning_text"]


def reasoning_text(source: dict[str, Any]) -> str:
    """ความคิดของโมเดลจาก message หรือ delta ของ OpenAI

    vLLM ใช้มาแล้วสองชื่อ: `reasoning_content` ในรุ่นเก่า · `reasoning` ในรุ่นใหม่ ·
    llama.cpp ใช้ `reasoning_content` · ดูชื่อเดียว = ครึ่งหนึ่งของฟลีตเงียบหาย
    """
    for field in ("reasoning_content", "reasoning"):
        value = source.get(field)
        if isinstance(value, str) and value:
            return value
    return ""
