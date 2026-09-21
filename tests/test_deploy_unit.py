"""ไฟล์ systemd unit ต้องยอมให้ service เขียนทุกที่ที่คอนโซลเขียนจริง

เคสจริง (2026-08-27): ทีมติดตั้งบนเครื่องใหม่แล้วหน้า Models ขึ้นว่า registry เป็น
read-only ปุ่ม Save กดไม่ได้ · unit ตั้ง `ProtectSystem=strict` ซึ่งทำให้ทั้งเครื่อง
เป็น read-only สำหรับ service แล้วเปิดเฉพาะที่อยู่ใน `ReadWritePaths=` — ซึ่งมีแค่
data กับ logs ทั้งที่การเพิ่ม/เปิด/ปิดโมเดลจากคอนโซลเขียนลง `config/models/`

ไม่มีใครเจอตอนพัฒนาเพราะเครื่อง dev รันในคอนเทนเนอร์ (OrbStack/LXC) ที่ systemd
ปิด sandbox ให้เอง — `ProtectSystem` กลายเป็น `no` และ `ReadWritePaths` ถูกล้าง
พฤติกรรมจึงต่างกันคนละขั้วระหว่างเครื่อง dev กับเครื่องลูกค้า

เทสนี้เทียบสองอย่างเข้าหากัน: path ที่โค้ดเขียนจริง กับ path ที่ unit อนุญาต
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parents[1] / "deploy/systemd/litegate.service"
BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts/bootstrap.sh"


def _unit_value(key: str) -> str:
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def test_the_console_can_write_the_registry_it_is_told_to_use():
    """ตัวติดตั้งจบด้วยการบอกให้ไปเพิ่มโมเดลที่คอนโซล — มันต้องเพิ่มได้จริง"""
    assert _unit_value("ProtectSystem") == "strict", "ถ้าเลิก sandbox แล้ว เทสนี้ต้องถูกเขียนใหม่"
    writable = _unit_value("ReadWritePaths").split()
    assert "/opt/litegate/config" in writable, (
        "คอนโซลเขียน config/models/<alias>.yaml ตอนเพิ่ม/เปิด/ปิดโมเดล · "
        "ไม่มี path นี้ใน ReadWritePaths แปลว่าติดตั้งใหม่บนเครื่องจริงแล้วปุ่ม Save กดไม่ได้"
    )


def test_every_path_the_service_writes_is_allowed():
    """data/logs ยังต้องอยู่ — เผลอลบบรรทัดใดบรรทัดหนึ่งแล้วพังคนละที่กัน"""
    writable = _unit_value("ReadWritePaths").split()
    for path in ("/opt/litegate/data", "/opt/litegate/logs"):
        assert path in writable


def test_the_hardened_registry_is_a_deliberate_choice_not_the_default():
    """ยังเลือกโหมดทะเบียน-อยู่-ใน-git ได้ แต่ต้องตั้งใจสั่ง ไม่ใช่เจอโดยไม่รู้ตัว"""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    assert "REGISTRY_READONLY" in script
    # ปิดได้ต้องปิดที่ ReadWritePaths ของ unit ที่ติดตั้งแล้ว ไม่ใช่แค่พิมพ์คำเตือน
    assert re.search(r"REGISTRY_READONLY.*\n(?:.*\n)?.*ReadWritePaths", script), (
        "REGISTRY_READONLY=1 ต้องแก้ ReadWritePaths ของ unit จริง"
    )


@pytest.mark.parametrize("key", ["NoNewPrivileges", "PrivateTmp", "ProtectKernelTunables"])
def test_the_rest_of_the_hardening_stays(key):
    """เปิด config ให้เขียนได้ ไม่ใช่ข้ออ้างให้ถอด sandbox ทั้งชุด"""
    assert _unit_value(key) == "true"


# ── จำนวน worker ต้องมาจากที่เดียวกับที่แอปอ่าน ──────────────────────────────────

def test_the_unit_does_not_hardcode_a_worker_count():
    """เคสจริง (2026-09-21): `scripts/bootstrap.sh` เขียน `GW_WORKERS=1` ลง .env ตาม
    ที่ 1.12.1 ประกาศว่าแก้แล้ว แต่ unit ยังเขียน `--workers 4` ไว้ตรง ๆ ·
    `GW_WORKERS` ถูกอ่านโดย `app.main:run` เท่านั้น ซึ่งเป็น console script ที่ unit นี้
    ไม่ได้เรียก (มันเรียก uvicorn ตรง ๆ) — **เครื่องที่ติดตั้งแบบ native จึงยังรัน
    4 worker โดยไม่มี Redis อยู่ดี** และ `max_concurrency: 1` ของทุก endpoint กลายเป็น
    4 คำขอพร้อมกันจริงที่ backend

    `docker/docker-compose.prod.yml` เจอบั๊กเดียวกันและแก้ไปแล้วพร้อมคอมเมนต์อธิบาย —
    ไม่มีใครย้อนกลับมาแก้ unit
    """
    exec_start = " ".join(
        line.strip().rstrip("\\").strip()
        for line in UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=") or line.startswith("    --")
    )
    assert "--workers ${GW_WORKERS}" in exec_start, exec_start
    assert not re.search(r"--workers\s+\d", exec_start), "ตัวเลขตายตัวกลับมาแล้ว"


def test_the_default_worker_count_cannot_be_shadowed_by_the_env_file():
    """systemd ให้บรรทัดที่มาทีหลังชนะ · `Environment=GW_WORKERS=1` จึงต้องอยู่ **ก่อน**
    `EnvironmentFile=` ไม่งั้นค่าที่ผู้ดูแลตั้งไว้ใน .env จะถูกค่าตั้งต้นทับ

    และต้องมีค่าตั้งต้นจริง ๆ เพราะ .env ของเครื่องที่ติดตั้งก่อนหน้านี้ไม่มีบรรทัดนั้นเลย —
    ไม่มีค่าตั้งต้น = `--workers` ว่าง = บริการไม่ขึ้นหลังอัปเกรด
    """
    lines = UNIT.read_text(encoding="utf-8").splitlines()
    default_at = next(i for i, x in enumerate(lines) if x.strip() == "Environment=GW_WORKERS=1")
    envfile_at = next(i for i, x in enumerate(lines) if x.startswith("EnvironmentFile="))
    assert default_at < envfile_at, "ค่าตั้งต้นต้องมาก่อน .env ไม่งั้นมันทับค่าของผู้ดูแล"


def test_bootstrap_repairs_a_worker_value_that_would_stop_the_service():
    """`.env` เดิมไม่เคยถูกเขียนทับโดยตั้งใจ — ข้อยกเว้นเดียวคือค่าที่ทำให้บริการไม่ขึ้น

    `--workers ${GW_WORKERS}` ที่ค่าว่างกลายเป็นอาร์กิวเมนต์เปล่า แล้ว uvicorn ตายทันที ·
    ปล่อยไว้ = อัปเกรดแล้วเกตเวย์ดับ ซึ่งแย่กว่าการแก้ค่าเดียวที่ผิดรูปอยู่แล้ว
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "GW_WORKERS=1" in text
    assert "^GW_WORKERS=[1-9][0-9]*" in text, "ต้องตรวจว่าเป็นตัวเลขบวก ไม่ใช่แค่ว่าไม่ว่าง"
    assert "sed -i 's/^GW_WORKERS=.*/GW_WORKERS=1/'" in text


def test_the_warning_reads_the_same_value_the_unit_uses():
    """`app/state.py` ตัดสินว่าจะเตือนจาก `settings.workers` ซึ่งมาจาก `GW_WORKERS` ·
    ตราบใดที่ unit ก็ใช้ค่าเดียวกัน จำนวน worker จริงกับคำเตือนจะตรงกันเสมอ

    ก่อนแก้: unit รัน 4, `settings.workers` = 1 → **คำเตือนไม่ขึ้นบนเครื่องที่ต้องการมัน
    ที่สุด** ซึ่งเป็นอาการที่มองไม่เห็นจนกว่าจะมีคนไปงงว่าทำไม backend โดนยิงพร้อมกันสี่คำขอ
    """
    from app.state import unshared_limit_warning

    assert unshared_limit_warning(redis=None, workers=4, is_production=True)
    assert not unshared_limit_warning(redis=None, workers=1, is_production=True)
