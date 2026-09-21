"""ค่าประมาณ token ต้องไม่ต่ำกว่าความจริงสำหรับภาษาไทย — เดิมต่ำกว่าเท่าตัว

วัดจริงกับ `qwen3-embedding-8b` บน dgx-spark04 (2026-09-22) โดยยิงผ่านเกตเวย์แล้วอ่าน
`usage.prompt_tokens` ที่ backend รายงานกลับมา · แปดชุด:

    อังกฤษล้วน 4.86 · โค้ด 3.26 · ไทยปนอังกฤษ 2.43 · ไทยล้วน 1.89 · ตัวเลข 1.16

ค่าเดิมคือ `CHARS_PER_TOKEN = 3.2` ตัวเดียว ซึ่ง **สูงไป 2.6 เท่าสำหรับภาษาไทย** ·
ยอดรวมของชุดนี้ออกมาเป็น **73% ของจริง** และต่ำกว่าจริงใน 5 จาก 8 เคส

**ทิศทางของความผิดพลาดไม่เท่ากัน** — ค่าประมาณนี้ถูกใช้ตอน backend ไม่รายงาน usage
มา แล้วเอาไปหักโควตา · ต่ำกว่าจริง = ช่องโหว่ที่คนใช้ GPU เกินโควตาได้ฟรี ·
เกินเล็กน้อย = เข้มกับผู้ใช้เกินไปหน่อย · เทสชุดนี้จึงยอมให้เกินได้ แต่ไม่ยอมให้ขาด
"""

from __future__ import annotations

import pytest

from app.core.multimodal import RequestProfile
from app.core.tokens import estimate_chars, estimate_text_tokens

# (ชื่อ, ข้อความ, token ที่ backend รายงานจริง)
MEASURED = [
    ("อังกฤษ", "The quick brown fox jumps over the lazy dog near the river bank today.", 16),
    ("อังกฤษ", "Please summarise the attached quarterly report and highlight any risks.", 13),
    ("ไทย", "แมวชอบกินปลาและนอนกลางวันบนหลังคาบ้านไม้เก่าในหมู่บ้านเล็ก", 29),
    ("ไทย", "กรุณาสรุปรายงานประจำไตรมาสแล้วชี้ให้เห็นความเสี่ยงที่สำคัญทั้งหมด", 36),
    ("ผสม", "ช่วย summarise รายงาน quarterly แล้วบอก risk ที่สำคัญหน่อย", 20),
    ("ผสม", "ติดตั้ง LMDS บน DGX Spark แล้วรัน benchmark ให้หน่อยครับ", 27),
    ("โค้ด", "def add(a, b):\n    return a + b  # simple helper used in tests", 19),
]


_IDS = [f"{k}-{i}" for i, (k, _, _) in enumerate(MEASURED)]


@pytest.mark.parametrize("kind,text,real", MEASURED, ids=_IDS)
def test_the_estimate_is_never_far_below_what_the_backend_counted(kind, text, real):
    """ยอมให้ต่ำกว่าจริงได้ไม่เกิน 15% · เกินได้ถึงเท่าตัว

    ไม่บังคับให้ตรงเป๊ะเพราะ tokenizer ของแต่ละโมเดลไม่เหมือนกัน — ที่บังคับคือ
    **ต้องไม่ขาดมาก** ซึ่งคือทิศทางที่ทำให้โควตารั่ว
    """
    got = estimate_chars(text)
    assert got >= real * 0.85, f"{kind}: ประมาณ {got} · จริง {real} — ต่ำกว่าจริง"
    assert got <= real * 2.0, f"{kind}: ประมาณ {got} · จริง {real} — เกินจริงมากเกินไป"


def test_thai_is_counted_far_denser_than_english():
    """หัวใจของการแก้: อักขระไทยจำนวนเท่ากันต้องได้ token มากกว่าอังกฤษหลายเท่า

    ค่าเดียวเดิมให้เลขเท่ากันเป๊ะสำหรับสองบรรทัดนี้ ซึ่งคือต้นเหตุทั้งหมด
    """
    thai = estimate_chars("ก" * 200)
    english = estimate_chars("a" * 200)
    assert thai > english * 2, f"ไทย {thai} · อังกฤษ {english}"


def test_digits_and_punctuation_are_not_counted_like_prose():
    """IP เวอร์ชัน วันที่ — tokenizer ตัดเกือบตัวต่อตัว · วัดได้ 1.16 อักขระ/token"""
    dense = estimate_chars("192.168.139.140:8080 2026-09-22 1,748 89.2")
    prose = estimate_chars("the quick brown fox jumps over a lazy dog")
    assert dense > prose


def test_a_profile_built_without_the_split_still_estimates_something():
    """`RequestProfile` ถูกสร้างขึ้นเองในบางที่ (เช่น count_tokens ย้อนกลับจาก token)
    โดยไม่ได้แยกชนิดอักขระ — ต้องไม่ระเบิดและต้องไม่ได้ 0"""
    profile = RequestProfile(text_chars=400)
    assert estimate_text_tokens(profile) == 100          # 400 / 4.0
    assert estimate_text_tokens(RequestProfile()) == 0


def test_token_ids_are_added_on_top_not_estimated():
    """ผู้เรียกที่ส่ง token id มาบอกจำนวนแน่นอนมาแล้ว — ห้ามเอาไปประมาณซ้ำ"""
    profile = RequestProfile(text_chars=0, pretokenized_tokens=512)
    assert estimate_text_tokens(profile) == 512
