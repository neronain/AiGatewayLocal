"""ข้อความแจ้งผู้เรียกทาง response header — สำหรับสิ่งที่เกตเวย์ *เปลี่ยน* ในคำขอของเขา

ทำไมต้องมี
----------
เกตเวย์แก้คำขอบางอย่างเองก่อนส่งให้ backend โดยมีเหตุผล (ลดเพดานคำตอบให้พอดีหน้าต่าง
context · ข้ามเครื่องมือที่ backend รันไม่ได้) แต่เดิมไม่มีอะไรบอกผู้เรียกเลย: ขอ
`max_tokens=4000` ได้คำตอบที่ถูกตัดที่ 256 พร้อม `finish_reason: "length"` และ header ชุด
เดียวกับคำขอปกติทุกตัว (ตรวจ 2026-10-06) — คนไล่ปัญหาไม่มีทางรู้ว่าตัวเลข 256 มาจากไหน

ทำไมเป็น ContextVar ไม่ใช่ค่าที่ส่งต่อกันเป็นทอด
----------------------------------------------
จุดที่ *รู้* ว่ามีการเปลี่ยนอยู่ลึก (`capability.validate_context_budget` ซึ่งถูกเรียกซ้ำต่อ
ความพยายามแต่ละครั้ง เพราะ fallback เปลี่ยนโมเดลได้) ส่วนจุดที่ *เขียน header* มีหกที่ในสาม
ไฟล์ (สตรีมกับไม่สตรีมของแต่ละ surface) · ร้อยค่าผ่านทุกชั้นคือรอวันที่ทางที่เจ็ดลืมใส่ —
รูปเดียวกับที่ `x-litegate-request-id` เลือกใส่ที่ middleware ที่เดียว

middleware เปิดสมุดจดหนึ่งเล่มต่อคำขอก่อนเรียกแอป (`begin`) · โค้ดข้างในจดลงเล่มนั้น
(`put`) · middleware อ่านตอน header พร้อม · ตัวแปรชี้ไปที่ dict ก้อนเดียวกันแม้ Starlette จะ
รันแอปใน task ลูก เพราะ context ถูกคัดลอกแบบตื้น — ของที่จดใน task ลูกจึงเห็นจากข้างนอก

สตรีม: header ออกไปก่อนไบต์แรกของ body แต่ไม่เป็นข้อจำกัดในทางปฏิบัติ — ทางเดินของสตรีม
(app/api/lifecycle.py: open_stream_for) เปิดสายถึง backend และสลับเครื่อง/โมเดลสำรองให้เสร็จ
*ก่อน* เริ่มตอบ การคิดเพดานของตัวที่เสิร์ฟจริงจึงเกิดก่อน middleware อ่านสมุด · สิ่งที่จดหลัง
ไบต์แรกออกไปแล้วเท่านั้นที่ไปไม่ถึงผู้เรียก และตอนนี้ไม่มีใครจดช่วงนั้น

นอกคำขอ (เทสที่เรียกฟังก์ชันตรง ๆ · งานเบื้องหลัง) ไม่มีสมุดเปิดอยู่ `put` จึงไม่ทำอะไร
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# เพดานคำตอบที่ส่งให้ backend ต่ำกว่าที่ผู้เรียกขอ (หรือต่ำกว่าเพดานของโมเดลเมื่อไม่ได้ขอ)
OUTPUT_CAP = "x-litegate-output-cap"
# นิยามเครื่องมือที่ตัวแปลข้ามไป เพราะ backend ในบ้านรันไม่ได้ (เครื่องมือที่ผู้ให้บริการรันเอง)
IGNORED = "x-litegate-ignored"
# ฟิลด์ของคำขอที่เกตเวย์เขียนใหม่ให้เข้ารูปก่อนส่ง (ตอนนี้: `response_format` ที่คีย์อยู่ผิดที่ —
# app/core/responseformat.py) · คั่นด้วย ", " ชิ้นละ `ต้นทาง->ปลายทาง` หรือ `ตำแหน่ง="ค่าที่เติม"`
ADJUSTED = "x-litegate-adjusted"

_book: ContextVar[dict[str, str] | None] = ContextVar("litegate_notices", default=None)


def begin() -> dict[str, str]:
    """เปิดสมุดของคำขอนี้ · คืน dict ที่จะกลายเป็น response header"""
    book: dict[str, str] = {}
    _book.set(book)
    return book


def put(header: str, value: str | None) -> None:
    """จด (หรือลบเมื่อ `value` เป็น None) · ค่าหลังสุดชนะ เพราะความพยายามหลังสุดคือตัวที่เสิร์ฟ"""
    book = _book.get()
    if book is None:
        return
    if value is None:
        book.pop(header, None)
    else:
        book[header] = value


@contextmanager
def muted() -> Iterator[None]:
    """ปิดสมุดชั่วคราว — สำหรับด่านที่ถูกเรียกเพื่อ *ลองถาม* ว่าโมเดลอื่นรับได้ไหม

    `rules.can_serve` รันด่านจริงกับโมเดลผู้สมัคร (routing rule · fallback · auto) · สิ่งที่ด่าน
    จดระหว่างนั้นเป็นเรื่องของโมเดลที่อาจไม่ได้ถูกเลือก จึงต้องไม่ไปถึงผู้เรียก
    """
    token = _book.set(None)
    try:
        yield
    finally:
        _book.reset(token)


def current() -> dict[str, str]:
    """สิ่งที่จดไว้ของคำขอนี้ (สำเนา) — ว่างเมื่ออยู่นอกคำขอ"""
    return dict(_book.get() or {})
