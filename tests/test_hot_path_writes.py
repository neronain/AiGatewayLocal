"""hot path ต้องไม่เขียน DB และไม่เปิด connection ใหม่โดยไม่จำเป็น

เทสสองข้อนี้วัด "จำนวนครั้ง" ไม่ใช่ "ผลลัพธ์" — เพราะทั้งสองอย่างที่แก้ไปไม่เปลี่ยน
พฤติกรรมที่ผู้ใช้เห็นเลย เปลี่ยนแค่ว่าทำงานหนักแค่ไหนต่อหนึ่งคำขอ · ถ้าไม่มีเทสแบบนี้
คนที่มาแก้ทีหลังจะเอา commit กลับเข้ามาได้โดยไม่มีอะไรแดง
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import auth as auth_mod
from app.core.routing import Router


def test_last_used_is_not_written_on_every_request():
    """เดิม commit ทุกคำขอ = write transaction หนึ่งรายการต่อหนึ่งคำขอ บน hot path"""
    assert auth_mod.LAST_USED_STAMP_INTERVAL >= timedelta(seconds=30), (
        "ช่วงประทับเวลาสั้นเกินไปจนแทบไม่ต่างจากเขียนทุกครั้ง")

    source = (auth_mod.__file__)
    text = open(source, encoding="utf-8").read()
    stamp = text[text.index("previous = _aware(api_key.last_used_at)"):]
    body = stamp[: stamp.index("return Principal")]
    assert "LAST_USED_STAMP_INTERVAL" in body, "การเขียนต้องอยู่ใต้เงื่อนไขช่วงเวลา"
    assert body.count("await session.commit()") == 1
    # commit ต้องอยู่ *ข้างใน* if ไม่ใช่ข้างนอก
    guard = body.index("if previous is None")
    assert body.index("await session.commit()") > guard


@pytest.mark.anyio
async def test_health_probes_reuse_one_client(monkeypatch):
    """เดิมสร้าง AsyncClient ใหม่ทุก probe ทุก endpoint ทุกรอบ = handshake ใหม่ตลอด"""
    import httpx

    made: list[int] = []
    real_init = httpx.AsyncClient.__init__

    def counting_init(self, *a, **kw):
        made.append(1)
        return real_init(self, *a, **kw)

    class _Registry:
        class snapshot:
            class gateway:
                health_check_timeout_seconds = 1.0
                health_check_interval_seconds = 3600
            models: dict = {}

    router = Router(_Registry())
    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_init)
    await router.start_health_checks()
    try:
        # ไม่มีโมเดลให้ probe แต่ client ต้องถูกสร้างไว้แล้วหนึ่งตัว ไม่ใช่สร้างตอน probe
        assert router._probe_client is not None
        assert made.count(1) == 1, "ต้องสร้าง client ครั้งเดียวตอน start"
        await router.probe_all()
        await router.probe_all()
        assert made.count(1) == 1, "probe ซ้ำต้องไม่สร้าง client เพิ่ม"
    finally:
        await router.stop_health_checks()
    assert router._probe_client is None, "ต้องปิด client ตอน stop ไม่ปล่อยรั่ว"
