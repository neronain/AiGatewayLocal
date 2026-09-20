"""ความต่างระหว่าง SQLite กับ PostgreSQL ที่โค้ดส่วนอื่นไม่ควรต้องรู้

เกตเวย์รันบน SQLite เป็นค่าเริ่มต้น (ลูกค้าเครื่องเดียวไม่ควรต้องลง Postgres) แต่
ลูกค้าองค์กรที่รันหลาย instance ต้องใช้ Postgres เพราะ SQLite ไม่มี replication และ
ไฟล์เดียวแชร์ข้าม instance ไม่ได้ · ทั้งสองจึงต้องทำงานได้จริง ไม่ใช่ "น่าจะได้"

ORM ของ SQLAlchemy พาเราไปได้เกือบหมด สิ่งที่เหลือคือจุดที่ *ภาษา SQL เองต่างกัน*
ซึ่งรวมไว้ที่ไฟล์นี้ที่เดียว — กระจายไปตาม call site เมื่อไร วันที่มีใครเพิ่มจุดที่ 4
แล้วลืม คือวันที่รายงานของลูกค้า Postgres ผิดโดยไม่มีอะไรแดง
"""

from __future__ import annotations

from sqlalchemy import Date
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

# ชื่อ dialect ที่เรารองรับอย่างเป็นทางการ · อย่างอื่นยังรันได้ถ้า SQLAlchemy รองรับ
# แต่เราไม่ได้ทดสอบ และ fallback ข้างล่างเป็นแค่ "เดาตามมาตรฐาน SQL"
SQLITE = "sqlite"
POSTGRESQL = "postgresql"


def dialect_name(url: str) -> str:
    """ชื่อ dialect จาก URL โดยไม่ต้องสร้าง engine — ใช้ตัดสินใจตอนตั้งค่า"""
    scheme = (url or "").split("://", 1)[0]
    return scheme.split("+", 1)[0].lower()


def is_sqlite(url: str) -> bool:
    return dialect_name(url) == SQLITE


def is_postgresql(url: str) -> bool:
    # SQLAlchemy รับทั้ง postgresql:// และ postgres:// (alias เก่า) — รับทั้งคู่
    return dialect_name(url) in (POSTGRESQL, "postgres")


class utc_date(FunctionElement):  # noqa: N801 - อ่านเป็นฟังก์ชัน SQL ไม่ใช่คลาส
    """วันตามปฏิทิน **UTC** ของคอลัมน์ timestamp หนึ่ง

    ทำไมต้องมี — `func.date(col)` ใช้ได้ทั้งสอง dialect แต่ได้คนละคำตอบ:

      * SQLite เก็บ DateTime เป็นสตริงเวลา UTC แบบไม่มี timezone (ไดรเวอร์ทิ้ง
        tzinfo ทิ้งตั้งแต่ตอน bind) · `date(col)` จึงตัดเป็นวันแบบ UTC ตรง ๆ
      * PostgreSQL เก็บเป็น `timestamptz` จริง · `date(col)` แปลงตามค่า `TimeZone`
        ของ session ก่อน ซึ่งปกติคือ timezone ของเครื่อง ไม่ใช่ UTC

    ผลคือรายงาน "ใช้ไปกี่ token ต่อวัน" ของลูกค้า Postgres จะเลื่อนขอบวันไปตาม
    timezone ของเซิร์ฟเวอร์ และคาบเกี่ยวกับ `WHERE ts >= since` ที่คิดแบบ UTC อยู่แล้ว
    — ตัวเลขของวันหัวกับวันท้ายจะเพี้ยนโดยไม่มีใครเห็นว่าเพี้ยน

    บังคับ UTC ทั้งสองฝั่ง แล้วรายงานของทุก deployment หมายความเหมือนกัน

    type เป็น `Date` ทั้งคู่โดยตั้งใจ — ผู้เรียกจึงได้ `datetime.date` เหมือนกัน
    ไม่ใช่สตริงบน SQLite และ date object บน Postgres
    """

    type = Date()
    name = "utc_date"
    inherit_cache = True


@compiles(utc_date)
def _utc_date_default(element, compiler, **kw) -> str:
    # SQLite และ dialect อื่นที่มี DATE() ตามมาตรฐาน · ค่าที่เก็บเป็น UTC อยู่แล้ว
    return f"date({compiler.process(element.clauses.clauses[0], **kw)})"


@compiles(utc_date, POSTGRESQL)
def _utc_date_postgresql(element, compiler, **kw) -> str:
    # AT TIME ZONE 'UTC' บน timestamptz คืน timestamp แบบไม่มี tz ที่เป็นเวลา UTC
    # แล้วค่อย cast เป็น date — ไม่ขึ้นกับค่า TimeZone ของ session
    inner = compiler.process(element.clauses.clauses[0], **kw)
    return f"(({inner}) AT TIME ZONE 'UTC')::date"
