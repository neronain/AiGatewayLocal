"""max_concurrency ต้องหมายความตามที่เขียนไว้ — หรือไม่ก็ต้องมีคนบอกว่ามันไม่

ตัวนับคำขอที่กำลังวิ่งอยู่แชร์ข้าม worker ได้ก็ต่อเมื่อมี Redis · ไม่มีแล้วแต่ละ worker
นับของตัวเอง `max_concurrency: 1` จึงกลายเป็น N ตัวพร้อมกันจริงที่ backend

เคสที่ทำให้ต้องมีไฟล์นี้: `scripts/bootstrap.sh` เคยเขียน `GW_WORKERS=4` คู่กับ
`GW_REDIS_URL=` ว่างเป็นค่าเริ่มต้น — **การติดตั้งแบบ native ทุกชุดจึง oversubscribe
backend ตั้งแต่วินาทีแรก** โดยไม่มีอะไรบอก ส่วน Docker ตั้งถูกมาตลอด
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.state import unshared_limit_warning

ROOT = Path(__file__).resolve().parents[1]


def test_many_workers_without_redis_is_called_out():
    text = unshared_limit_warning(redis=False, workers=4, is_production=True)
    assert text, "เงียบตรงนี้คือปล่อยให้ max_concurrency โกหกต่อไป"
    assert "GW_REDIS_URL" in text and "GW_WORKERS=1" in text, "ต้องบอกทางแก้ทั้งสองทาง"
    assert "4" in text, "ต้องบอกว่าเป็นกี่เท่าจริง ๆ ไม่ใช่บอกแค่ว่ามีปัญหา"


@pytest.mark.parametrize(
    "redis, workers, why",
    [
        (True, 4, "มี Redis = ตัวนับแชร์กันจริง หลาย worker ไม่เป็นไร"),
        (False, 1, "worker เดียว = ตัวนับของมันคือตัวนับทั้งหมด"),
    ],
)
def test_the_safe_combinations_say_nothing(redis, workers, why):
    assert unshared_limit_warning(redis=redis, workers=workers, is_production=True) == "", why


def test_development_says_nothing_because_it_runs_one_worker_anyway():
    """`main.py` บังคับ workers=1 เมื่อไม่ใช่ production — เตือนก็ทำให้คนชินกับคำเตือนที่ไม่จริง"""
    assert unshared_limit_warning(redis=False, workers=8, is_production=False) == ""


def test_bootstrap_does_not_ship_the_broken_combination():
    """เตือนตอนรันอย่างเดียวไม่พอ ถ้าตัวติดตั้งของเราเองยังสร้างค่าผสมนั้นให้ทุกครั้ง"""
    env_block = (ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    workers = [ln for ln in env_block.splitlines() if ln.startswith("GW_WORKERS=")]
    redis = [ln for ln in env_block.splitlines() if ln.startswith("GW_REDIS_URL=")]
    assert workers == ["GW_WORKERS=1"], f"bootstrap เขียน {workers} ไว้คู่กับ {redis}"
    assert redis == ["GW_REDIS_URL="], "ถ้าวันหนึ่งใส่ Redis ให้เลย ค่า workers ก็ปรับขึ้นได้"


def test_the_warning_text_matches_what_bootstrap_tells_people_to_do():
    """คำแนะนำสองที่ต้องไม่ขัดกัน — คนอ่านเจอคนละอย่างแล้วไม่รู้จะเชื่ออันไหน"""
    text = unshared_limit_warning(redis=False, workers=4, is_production=True)
    comment = (ROOT / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "GW_REDIS_URL" in comment and "GW_WORKERS" in comment
    assert "GW_REDIS_URL" in text
