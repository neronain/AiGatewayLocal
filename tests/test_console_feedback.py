"""คอนโซลตอบกลับตรงที่ผู้ใช้กด ไม่ใช่ตรงที่โครงหน้าเว็บวางไว้

ลูกค้าแจ้งว่า "หน้า assistant กด Deploy tool แล้วไม่มีอะไรเกิดขึ้น" · ปุ่มทำงาน
ปกติ คำตอบก็มา แต่คำตอบทุกอย่างของคอนโซล — ทั้ง error และข้อความสำเร็จ — ไปลง
กล่องเดียวที่บนสุดของ <main> ซึ่งบนแท็บ assistant อยู่สูงกว่าปุ่มราวสองพันเจ็ดร้อย
พิกเซล กดแล้วจึงไม่มีอะไรเปลี่ยนในส่วนของหน้าที่คนกำลังมองอยู่จริงๆ
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_the_answer_follows_the_reader_down_the_page():
    block = CSS.split("#error {", 1)[1].split("}", 1)[0]
    assert "position: sticky" in block, "กล่องคำตอบต้องเกาะจอ ไม่ใช่ค้างอยู่หัวหน้า"
    assert "z-index" in block, "ต้องอยู่เหนือเนื้อหา ไม่งั้นโดนการ์ดทับ"


def test_it_parks_under_whatever_height_the_header_actually_is():
    """จอแคบแถวเมนูตัดบรรทัดแล้ว header สูงขึ้น — ตรึงตัวเลขตายตัวไว้จะไปบังเมนู"""
    body = JS.split("function banner(", 1)[1].split("\n}", 1)[0]
    assert "offsetHeight" in body
    assert "querySelector('header')" in body


def test_a_message_can_be_put_away():
    """แถบนี้ลอยอยู่เหนือเนื้อหา ค้างไว้ก็บังของข้างหลัง"""
    body = JS.split("function banner(", 1)[1].split("\n}", 1)[0]
    assert "onclick" in body and "innerHTML = ''" in body


def test_a_confirmation_leaves_but_a_failure_stays():
    """คนที่พลาดมักต้องอ่านซ้ำ · คนที่สำเร็จอ่านครั้งเดียวก็พอ"""
    body = JS.split("function banner(", 1)[1].split("\n}", 1)[0]
    assert "kind === 'ok'" in body and "setTimeout" in body


def test_a_block_before_the_gateway_says_so_instead_of_request_failed():
    """403 ที่ไม่ใช่ JSON ไม่ได้มาจากเกตเวย์ — nginx ตอบแทนตาม allow list

    คอนโซลเรียก /admin/ เกือบทุกปุ่ม พอโดนบล็อกจึงพังทั้งหน้าพร้อมกัน และสิ่งที่
    ขึ้นมาคือ "403: request failed" ซึ่งไม่ได้ชี้ไปที่ไฟล์ไหนหรือแก้อย่างไรเลย
    """
    body = JS.split("async function api(", 1)[1].split("\nconst post", 1)[0]
    assert "JSON.parse" in body
    assert "response.status === 403" in body
    assert "allow list" in body
    assert "litegate.conf" in body, "ต้องบอกไฟล์ที่ต้องไปแก้"
