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
