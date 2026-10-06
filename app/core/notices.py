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

ข้อจำกัดที่ต้องรู้: header ออกไปก่อนไบต์แรกของ body · สำหรับสตรีม สิ่งที่จดหลังสตรีมเริ่ม
(เช่นเพดานของโมเดลสำรองที่ถูกสลับเข้ามากลางทาง) จึงไปไม่ถึงผู้เรียก — header ของสตรีมคือ
ค่าที่รู้ตอนเริ่ม เหมือน `x-litegate-served-by`

นอกคำขอ (เทสที่เรียกฟังก์ชันตรง ๆ · งานเบื้องหลัง) ไม่มีสมุดเปิดอยู่ `put` จึงไม่ทำอะไร
"""

from __future__ import annotations

from contextvars import ContextVar

# เพดานคำตอบที่ส่งให้ backend ต่ำกว่าที่ผู้เรียกขอ (หรือต่ำกว่าเพดานของโมเดลเมื่อไม่ได้ขอ)
OUTPUT_CAP = "x-litegate-output-cap"

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


def current() -> dict[str, str]:
    """สิ่งที่จดไว้ของคำขอนี้ (สำเนา) — ว่างเมื่ออยู่นอกคำขอ"""
    return dict(_book.get() or {})
