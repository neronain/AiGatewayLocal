"""JSON เข้า-ออกของเกตเวย์ — ใช้ orjson ถ้ามี ไม่มีก็ json ของ stdlib

ทำไมถึงคุ้ม
-----------
เกตเวย์ไม่ได้ *คิด* อะไรเลย มันรับ JSON แปลงแล้วส่งต่อ · งาน CPU เกือบทั้งหมดต่อหนึ่ง
คำขอจึงเป็นการ encode/decode JSON ล้วน ๆ และมันอยู่บน event loop:

  * คำตอบแบบไม่สตรีม — ตัว body ทั้งก้อนถูก dumps ครั้งเดียว คำตอบยาว ๆ คือหลายสิบ KB
  * สตรีม — ทุก chunk ที่ backend ส่งมาต้อง loads แล้ว dumps กลับ อย่างละครั้ง
    คำตอบหนึ่งคำตอบจึงเป็นหลักพันรอบ ต่อหนึ่งคำขอ ต่อทุกคำขอที่กำลังวิ่งพร้อมกัน

ทำไมเป็นของเสริม ไม่ใช่ dependency หลัก
--------------------------------------
orjson เป็น wheel ที่คอมไพล์แล้ว (Rust) · ลูกค้าที่รัน air-gapped หรืออยู่บน
สถาปัตยกรรมที่ไม่มี wheel ต้องยังลงเกตเวย์ได้ตามปกติ · ไม่มี orjson = ช้ากว่าเดิมนิดหน่อย
ไม่ใช่รันไม่ได้ และ **ผลลัพธ์ต้องเหมือนกันทุกตัวอักษร** เทสยืนยันข้อนี้ทั้งสองทาง

ข้อต่างที่รู้ตัวและยอมรับ
------------------------
* NaN / Infinity — stdlib โยน ValueError, orjson เขียนเป็น `null` · เข้าไม่ถึงในทางปฏิบัติ
  เพราะ JSON ไม่มีลิเทอรัลสองตัวนี้ ของที่ parse เข้ามาจึงมีไม่ได้
* ความลึกของโครงสร้าง — orjson หยุดที่ 254 ชั้น, stdlib ที่ ~1000 · `dumps()` ตก
  กลับไปใช้ stdlib เองเมื่อ orjson ปฏิเสธ จึงไม่มีคำขอไหนพังเพราะเลือกตัว encoder
* `orjson.JSONDecodeError` สืบทอดจาก `json.JSONDecodeError` และ `ValueError` — โค้ด
  ที่ดัก `json.JSONDecodeError` อยู่แล้วจึงยังดักได้เหมือนเดิม
"""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import JSONResponse

try:  # pragma: no cover - ทางไหนถูกเดินขึ้นกับว่าลง [speed] ไว้หรือเปล่า
    import orjson

    HAS_ORJSON = True
except ImportError:  # pragma: no cover
    orjson = None  # type: ignore[assignment]
    HAS_ORJSON = False


# OPT_NON_STR_KEYS: stdlib แปลงคีย์ที่เป็น int/float เป็นสตริงให้เอง ส่วน orjson
# ปฏิเสธถ้าไม่เปิดตัวเลือกนี้ · เปิดไว้เพื่อให้สองทางทำตัวเหมือนกัน
_OPTS = (orjson.OPT_NON_STR_KEYS if HAS_ORJSON else 0)
_OPTS_SORTED = _OPTS | (orjson.OPT_SORT_KEYS if HAS_ORJSON else 0)


def dumpb(value: Any, *, default=None) -> bytes:
    """JSON เป็น bytes แบบกระชับ UTF-8 — เท่ากับ json.dumps(ensure_ascii=False).encode()

    คืน bytes ไม่ใช่ str เพราะปลายทางเกือบทุกที่ (body ของ response, payload ของ SSE)
    ต้องการ bytes อยู่แล้ว · การเด้งผ่าน str คือการ encode/decode ทิ้งเปล่า ๆ หนึ่งรอบ
    """
    if HAS_ORJSON:
        try:
            return orjson.dumps(value, default=default, option=_OPTS)
        except (TypeError, ValueError):
            # โครงสร้างที่ orjson ปฏิเสธ (ลึกเกิน 254 ชั้น เป็นต้น) — ให้ stdlib ลองต่อ
            # ถ้า stdlib ก็ปฏิเสธ ข้อผิดพลาดที่ผู้เรียกได้จะเป็นตัวเดียวกับที่เคยได้มาตลอด
            pass
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=default
    ).encode("utf-8")


def dumps(value: Any, *, default=None) -> str:
    return dumpb(value, default=default).decode("utf-8")


def canonical(value: Any, *, default=None) -> bytes:
    """รูปแบบเดียวสำหรับค่าเดียว — คีย์เรียงแล้ว ใช้ทำ cache key / ลายนิ้วมือ

    ต้องเสถียรภายในกระบวนการเดียวเท่านั้น (ไม่ได้ใช้เทียบข้ามเครื่อง) แต่การเรียงคีย์
    ทำให้ dict ที่มีของเหมือนกันแต่ลำดับต่างกันได้ key เดียวกัน ซึ่งเป็นเรื่องของ hit rate
    """
    if HAS_ORJSON:
        try:
            return orjson.dumps(value, default=default, option=_OPTS_SORTED)
        except (TypeError, ValueError):
            pass
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=default
    ).encode("utf-8")


def loads(data: str | bytes | bytearray) -> Any:
    """แปลง JSON · โยน json.JSONDecodeError เมื่ออ่านไม่ได้ (ทั้งสองทาง)"""
    if HAS_ORJSON:
        return orjson.loads(data)
    if isinstance(data, bytes | bytearray):
        data = data.decode("utf-8")
    return json.loads(data)


class FastJSONResponse(JSONResponse):
    """คำตอบ JSON ที่ encode ด้วย orjson เมื่อมี

    Starlette ใช้ `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`
    ซึ่งได้ไบต์ชุดเดียวกับ `dumpb()` เป๊ะ · ต่างกันแค่ความเร็ว จึงสลับได้โดยไม่มีอะไร
    ที่ผู้เรียกมองเห็นเปลี่ยน

    ตั้งเป็น default_response_class ของแอป ทุก endpoint ที่ `return dict` จึงได้ไปด้วย
    โดยไม่ต้องแก้ทีละที่ · จุดที่สร้าง Response เองยังต้องเขียนชื่อคลาสนี้ตรง ๆ
    """

    def render(self, content: Any) -> bytes:
        return dumpb(content)
