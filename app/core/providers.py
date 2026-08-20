"""ผู้ให้บริการโมเดลแบบออนไลน์ — ค่าตั้งต้นที่รู้อยู่แล้ว ไม่ต้องให้ผู้ใช้ไปค้นเอง

LiteGate เกิดมาเพื่อคุมโมเดลที่รันบนเครื่องของโรงเรียนเอง และนั่นยังเป็นเรื่องหลัก ·
แต่ของจริงคือทีมต้องผสม: ใช้ของตัวเองเป็นหลัก แล้วเรียกคลาวด์เฉพาะงานที่เครื่องตัวเอง
ทำไม่ไหว หรือช่วงที่ยังไม่ได้ซื้อเครื่อง

**ส่วนใหญ่ไม่ต้องเขียนโค้ดใหม่เลย** — OpenRouter, MiniMax, DeepSeek, Groq, Together
และแม้แต่ Gemini ล้วนมี endpoint ที่พูด OpenAI Chat Completions ได้ · ที่ขาดคือความรู้
ว่า base URL คืออะไร ต้องยิงไปที่ path ไหน และคีย์ส่งด้วย header แบบไหน ซึ่งเป็นสิ่งที่
ผู้ใช้ต้องไปเปิดเอกสารทีละเจ้า · ตารางนี้ตอบแทนให้

ตัวที่ต่างจริงคือ **Anthropic**: ส่งคีย์ด้วย `x-api-key` ไม่ใช่ `Authorization: Bearer`
และบังคับ header `anthropic-version` · จึงต้องมีชนิดของ auth ไม่ใช่แค่ base URL

**ไม่มีคีย์อยู่ในไฟล์นี้** — เก็บชื่อ env var ไว้เฉย ๆ ตามกติกาเดิมของ Endpoint
(`api_key_env`) ค่าจริงอยู่ในสภาพแวดล้อมของ process ไม่เคยถูกเขียนลง config หรือ log
"""

from __future__ import annotations

from dataclasses import dataclass

# วิธีส่งคีย์ขึ้นต้นน้ำ
AUTH_BEARER = "bearer"        # Authorization: Bearer <key>  — ส่วนใหญ่ใช้แบบนี้
AUTH_X_API_KEY = "x-api-key"  # x-api-key: <key>             — Anthropic


@dataclass(frozen=True)
class Provider:
    """ค่าตั้งต้นของผู้ให้บริการหนึ่งเจ้า — ผู้ใช้ยังแก้ทับได้ทุกช่องหลังเลือก"""

    id: str
    label: str
    base_url: str
    auth: str = AUTH_BEARER
    # คลาวด์ไม่มี /health · GET /models โดยไม่ใส่คีย์ตอบ 401 ซึ่งพอบอกได้ว่า "ต่อถึง"
    # (ตัวตรวจถือว่าอะไรที่ต่ำกว่า 500 คือถึงแล้ว)
    health_path: str = "/models"
    speaks_openai: bool = True
    speaks_anthropic: bool = False
    # หมายเหตุที่คนตั้งค่าต้องรู้ก่อนกด save — ขึ้นในหน้าเว็บใต้ตัวเลือก
    note: str = ""
    docs: str = ""


# เรียงตามที่ทีมจะใช้จริงก่อน แล้วค่อยตัวเผื่อ
CLOUD: dict[str, Provider] = {
    "gemini": Provider(
        "gemini", "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        note="ใช้ทาง OpenAI-compatible ของ Google · ชื่อโมเดลเช่น gemini-2.5-flash",
        docs="https://ai.google.dev/gemini-api/docs/openai",
    ),
    # MiniMax แยกเป็นคนละระบบตามภูมิภาค ไม่ใช่แค่คนละโดเมน — คีย์ของฝั่งหนึ่งใช้กับอีก
    # ฝั่งไม่ได้ (ตอบ invalid api key) จึงต้องเป็นคนละตัวเลือกและคนละ env var
    # ไม่ใช่หมายเหตุให้ผู้ใช้ไปแก้ URL เอง
    "minimax": Provider(
        "minimax", "MiniMax (Global)",
        "https://api.minimax.io/v1",
        note="บัญชี minimax.io · คีย์คนละใบกับฝั่งจีน",
        docs="https://www.minimax.io/platform/document",
    ),
    "minimax-cn": Provider(
        "minimax-cn", "MiniMax (จีนแผ่นดินใหญ่)",
        "https://api.minimaxi.com/v1",
        note="บัญชี minimaxi.com · คีย์คนละใบกับฝั่ง Global",
        docs="https://platform.minimaxi.com/document",
    ),
    "openrouter": Provider(
        "openrouter", "OpenRouter",
        "https://openrouter.ai/api/v1",
        note="ชื่อโมเดลต้องมีชื่อผู้ผลิตนำหน้า เช่น anthropic/claude-sonnet-4",
        docs="https://openrouter.ai/docs",
    ),
    "openai": Provider(
        "openai", "OpenAI",
        "https://api.openai.com/v1",
        docs="https://platform.openai.com/docs/api-reference",
    ),
    "anthropic": Provider(
        "anthropic", "Anthropic (Claude)",
        "https://api.anthropic.com/v1",
        auth=AUTH_X_API_KEY,
        speaks_openai=False,
        speaks_anthropic=True,
        note="ส่งคีย์ด้วย x-api-key และต้องมี anthropic-version — เกตเวย์ใส่ให้เอง",
        docs="https://docs.anthropic.com/en/api/messages",
    ),
    "deepseek": Provider(
        "deepseek", "DeepSeek",
        "https://api.deepseek.com/v1",
        docs="https://api-docs.deepseek.com",
    ),
    "groq": Provider(
        "groq", "Groq",
        "https://api.groq.com/openai/v1",
        docs="https://console.groq.com/docs/openai",
    ),
    "together": Provider(
        "together", "Together AI",
        "https://api.together.xyz/v1",
        docs="https://docs.together.ai",
    ),
}

# เวอร์ชันของ Anthropic API ที่เราส่งไปด้วยเสมอ · ไม่ใช่ค่าที่ผู้ใช้ต้องรู้
ANTHROPIC_VERSION = "2023-06-01"


def auth_style(server_type: str) -> str:
    """คีย์ของ endpoint ชนิดนี้ส่งด้วย header แบบไหน"""
    provider = CLOUD.get((server_type or "").lower())
    return provider.auth if provider else AUTH_BEARER


def is_cloud(server_type: str) -> bool:
    return (server_type or "").lower() in CLOUD


def catalogue() -> list[dict]:
    """รายการให้หน้าเว็บทำ dropdown + เติมค่าให้อัตโนมัติ — ไม่ hardcode ซ้ำฝั่ง client"""
    return [
        {
            "id": p.id, "label": p.label, "base_url": p.base_url,
            "health_path": p.health_path, "auth": p.auth,
            "speaks_openai": p.speaks_openai, "speaks_anthropic": p.speaks_anthropic,
            "note": p.note, "docs": p.docs,
            # ชื่อต้องใช้เป็นตัวแปรสภาพแวดล้อมได้ · id อย่าง "minimax-cn" มีขีดกลาง
            "suggested_env": f"{p.id.upper().replace('-', '_')}_API_KEY",
        }
        for p in CLOUD.values()
    ]
