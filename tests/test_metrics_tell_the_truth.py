"""metrics ต้องวัดสิ่งที่มันอ้างว่าวัด

gateway นี้เสิร์ฟ streaming เป็นหลัก · `call_next` ของ Starlette คืนค่าเมื่อ **header
พร้อม** ไม่ใช่เมื่อ body ไหลจบ · ตัวเลขที่นับตรงนั้นจึงผิดแทบทุกแถว และผิดแบบที่ดู
เผิน ๆ เหมือนใช้ได้ ซึ่งแย่กว่าไม่มีตัวเลขเลย
"""
from __future__ import annotations

import re

from app import main as main_mod

SOURCE = open(main_mod.__file__, encoding="utf-8").read()


def _middleware_body() -> str:
    start = SOURCE.index("async def request_context(")
    end = SOURCE.index("@app.exception_handler(GatewayError)", start)
    return SOURCE[start:end]


def test_in_flight_is_not_decremented_before_the_body_is_sent():
    """stream ที่กำลังวิ่งต้องมองเห็นได้ — เดิมลดค่าก่อน body เริ่มไหลด้วยซ้ำ"""
    body = _middleware_body()
    assert "body_iterator" in body, "ต้องห่อ body_iterator ไม่งั้นนับจบเร็วเกินจริง"
    # IN_FLIGHT.dec() ต้องอยู่ใน settle() ที่ถูกเรียกตอน body จบ ไม่ใช่ลอยอยู่หลัง call_next
    assert body.count("IN_FLIGHT.dec()") == 1, "ต้องมีจุดลดค่าจุดเดียว"
    after_call = body[body.index("response = await call_next(request)"):]
    stray = re.search(r"^\s{8}IN_FLIGHT\.dec\(\)", after_call, re.M)
    assert stray is None, "IN_FLIGHT.dec() ต้องไม่อยู่นอก settle()"


def test_duration_is_observed_once_per_request():
    body = _middleware_body()
    assert body.count("LATENCY.labels(path, model).observe(") == 1
    assert "settled" in body, "ต้องกันไม่ให้นับซ้ำเมื่อทั้ง except และ finally ทำงาน"


def test_a_disconnected_stream_still_settles():
    """client หลุดกลางทางแล้วไม่นับจบ = gauge ค้างสูงถาวรจนกว่าจะรีสตาร์ต"""
    body = _middleware_body()
    counted = body[body.index("async def counted_body("):]
    assert "finally:" in counted, "ต้องนับจบใน finally ไม่ใช่หลังลูปจบตามปกติ"


def test_ttft_is_exported_not_only_stored():
    """TTFT เป็น SLI หลักของ gateway — เดิมคำนวณแล้วเขียนลง DB อย่างเดียว"""
    assert hasattr(main_mod, "TTFT"), "ต้องมี histogram ของ TTFT"
    openai_src = open(
        __import__("app.api.openai", fromlist=["x"]).__file__, encoding="utf-8").read()
    assert "TTFT.labels(" in openai_src, "finalize ต้อง observe TTFT ออกไปจริง"
    assert "ttft_ms / 1000" in openai_src, "Prometheus ใช้หน่วยวินาที ไม่ใช่มิลลิวินาที"


def _gauge(text: str, name: str) -> float:
    for line in text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    raise AssertionError(f"{name} ไม่อยู่ใน /metrics")


def test_a_scrape_reports_readiness_without_anyone_calling_readyz(client):
    """gauge ต้องถูกวัดตอน scrape — เดิมตั้งจาก /readyz เท่านั้น

    เคสจริง 2026-10-05 (GW_WORKERS=4): /readyz ตอบ 8 โมเดล endpoint 2/9 ขณะที่
    /metrics ตอบ 0 ทุกตัว · กฎเตือน `endpoints_healthy < endpoints_total` จึงไม่ดัง

    ตั้งค่าหลอกไว้ก่อน เพราะ registry ของ prometheus เป็นของทั้ง process — เทสอื่นที่
    เรียก /readyz มาก่อนจะทำให้เทสนี้ผ่านเองโดยที่โค้ดยังผิดอยู่
    """
    for gauge in (main_mod.READY, main_mod.ENDPOINTS_HEALTHY,
                  main_mod.ENDPOINTS_TOTAL, main_mod.MODELS_LOADED):
        gauge.set(-1)

    scraped = client.get("/metrics").text
    truth = client.get("/readyz").json()

    assert truth["models_loaded"] > 0 and truth["endpoints_total"] > 0, "fixture ต้องมีของให้วัด"
    assert _gauge(scraped, "litegate_models_loaded") == truth["models_loaded"]
    assert _gauge(scraped, "litegate_endpoints_total") == truth["endpoints_total"]
    assert _gauge(scraped, "litegate_endpoints_healthy") == truth["endpoints_healthy"]
    assert _gauge(scraped, "litegate_ready") == (1 if truth["ready"] else 0)
