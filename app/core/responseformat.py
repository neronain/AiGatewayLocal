"""รูปของ `response_format` (structured output) ก่อนส่งให้ backend — ซ่อมที่ซ่อมได้ ปฏิเสธที่เหลือ

ทำไมต้องมีไฟล์นี้
-----------------
รูปมาตรฐานของ chat completions คือ

    {"type": "json_schema", "json_schema": {"name": ..., "strict": ..., "schema": {...}}}

client ส่งมาเพี้ยนจากนี้ได้หลายแบบ (คัดลอกจากเอกสารรุ่นเก่า · ปนกับรูปของ function calling หรือ
ของ Responses API) และ backend ในบ้าน **ไม่ปฏิเสธ** — ตอบ 200 โดยไม่ใช้ schema · วัดกับ
llama.cpp จริง 2026-10-09 (b11046 · ยิงตรง · ถามให้ "ตอบคำว่า hello" พร้อม schema ที่บังคับ
`{"color_code": "Q7"|"Z9"}` การทำตาม schema จึงดูออกจากคำตอบทันที):

    รูปที่ส่ง                                    สถานะ  คำตอบ                   schema ถูกใช้
    รูปมาตรฐาน (strict true / ไม่มี strict)      200    {"color_code": "Q7"}    ใช่
    ไม่มี name                                   200    {"color_code": "Q7"}    ใช่
    strict อยู่ข้าง type                         200    {"color_code": "Q7"}    ใช่ (ไม่อ่าน strict)
    json_schema.parameters แทน .schema           200    {"answer": "hello"}     **ไม่**
    แบน: name/schema/strict ข้าง type            200    {"answer": "hello"}     **ไม่**
    json_schema ไม่มี schema · มีแต่ type        200    {"answer": "hello"}     **ไม่** (JSON อะไรก็ได้)
    มี json_schema แต่ไม่มี type                 200    hello                   **ไม่** (ข้อความธรรมดา)
    schema เป็นสตริง                             200    hello                   **ไม่**
    response_format เป็นสตริง "json_object"      200    hello                   **ไม่**
    {"type": "json_object"}                      200    {"answer": "hello"}     -
    {"type": "json_object", "schema": {...}}     200    {"color_code": "Q7"}    ใช่ (ส่วนขยาย)
    {"type": "json"}                             400    "response_format type must be one of
                                                        "text" or "json_object", but got: json"

แถวที่ "ไม่" คือของอันตราย: client ได้ 200 แล้วไปพังตอน parse คำตอบตาม schema ของตัวเอง โดยไม่มี
อะไรบอกว่า backend ไม่เคยเห็น schema นั้น · vLLM ไม่ได้วัด (ห้ามยิง) อ่านจากซอร์ส v0.19.1:
`json_schema.name` บังคับ · ไม่มี `json_schema` = 400 · `parameters` = 400 ที่ข้อความพูดถึง
"structured outputs constraint" ไม่ได้เอ่ยชื่อฟิลด์ที่ผิด — คนละอาการ ปัญหาเดียวกัน

สิ่งที่ทำ
---------
* **ย้ายเข้าที่**: คีย์ของ `json_schema` ที่ไปอยู่ข้าง `type` (`strict` · `name` · `description` ·
  `schema` · `parameters`) · ถ้าข้างในมีอยู่แล้ว ตัวข้างในชนะ ตัวข้างนอกถูกทิ้ง
* **`parameters` = `schema`** (ชื่อของ function calling)
* **เติม** `type` เมื่อมี `json_schema`/`schema` แต่ลืม type · เติม `name: "response"` เมื่อไม่มี
  (ค่าเดียวกับที่ตัวแปลของ /v1/responses และ /v1/messages ใช้อยู่)
* **ปฏิเสธ** (400 บอกฟิลด์) เมื่อไม่มี schema ให้ย้าย หรือชนิดผิดจนเดาเจตนาไม่ได้
* ทุกอย่างที่แก้ถูกรายงานกลับ — ผู้เรียก (app/core/params.py) ใส่ลง `x-litegate-adjusted`

สิ่งที่ **ไม่** ทำ
------------------
* ไม่แตะ `text` · `json_object` (รวม `schema` ที่แนบมากับ json_object ซึ่ง llama.cpp ใช้จริง) ·
  ชนิดที่ไม่รู้จัก (เช่น `structural_tag` ของ vLLM) — backend ตัดสินเอง และ llama.cpp ตอบ 400
* **ไม่เขียน schema ของผู้เรียก** · OrcaRouter-Lite เติม `additionalProperties: false` ให้ทุก
  object และเขียน `required` ใหม่เป็นทุกคีย์เมื่อ `strict: true` (กติกาของ OpenAI) · วัดกับ
  llama.cpp 2026-10-09 ขอสามคีย์ a/b/c ด้วย schema ที่ประกาศแค่ `a`:

      strict true  · ไม่ระบุ additionalProperties      {"a": 1}
      strict true  · additionalProperties: false       {"a": 1}
      strict false · ไม่ระบุ                            {"a": 1}
      strict true  · additionalProperties: true        {"a": 1, "b": 2 …

  object ที่ไม่ระบุ **ปิดอยู่แล้ว** และ `strict` ไม่มีผล — การเติมจึงไม่ได้ช่วย · ส่วนคีย์ที่อยู่
  นอก `required` เป็นตัวเลือกจริง (schema {a, b} required [a] ได้ `{"a": 1}`) การเขียน
  `required` ใหม่จึง *เปลี่ยนคำตอบ* · schema ที่ OpenAI ตัวจริงไม่รับจะได้ 400 ของ OpenAI เอง
  ซึ่งบอกชื่อฟิลด์อยู่แล้ว

ผลต่อแคชคำตอบ: ตัวปรับรูปทำงานกับ body ตั้งแต่ด่านแรก · key ของแคช (core/responsecache.py)
สร้างจาก payload ที่จะส่งจริง จึงเกิด *หลัง* การปรับ — สองคำขอที่ถึง backend เป็นไบต์เดียวกันใช้
คำตอบเดียวกัน และ schema ที่ต่างกันจริงยังเป็นคนละ key

ดัดแปลงจาก OrcaRouter-Lite `app/response_format.py` (MIT · commit 4cacea8): โครงของการย้ายคีย์
มาจากที่นั่น · ส่วนที่เขียน schema ถูกตัดออกด้วยเหตุข้างบน · การปฏิเสธกับรายงานเป็นของที่นี่
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode, GatewayError

_FIELD = "response_format"
_WRAPPER = f"{_FIELD}.json_schema"
# คีย์ที่ที่อยู่ของมันคือข้างใน `json_schema` · เรียงตามลำดับที่อยากให้รายงานออกมา
_INNER_KEYS = ("name", "description", "strict", "schema", "parameters")
_SHAPE = '{"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}'


def _bad(param: str, problem: str) -> GatewayError:
    return GatewayError(ErrorCode.INVALID_REQUEST, f"'{param}' {problem}", param=param)


def normalize(wanted: Any) -> tuple[Any, list[str]]:
    """คืน (รูปที่จะส่งให้ backend, รายการสิ่งที่แก้) · ไม่ได้แก้ = คืนก้อนเดิม ไม่ใช่สำเนา

    รายการสิ่งที่แก้อ่านได้ทีละชิ้น: `ต้นทาง->ปลายทาง` (ย้าย) · `ต้นทาง->(dropped)` (ซ้ำกับ
    ตัวที่อยู่ถูกที่) · `ตำแหน่ง="ค่า"` (เติมให้)
    """
    if not isinstance(wanted, dict):
        raise _bad(_FIELD, f'must be an object such as {{"type": "json_object"}} or {_SHAPE}.')
    if not wanted:
        return wanted, []  # ก้อนว่าง = ไม่ได้ขออะไร ไม่มีอะไรจะหายเงียบ ๆ

    kind = wanted.get("type")
    if kind is None:
        # ลืม type แต่เจตนาชัด — llama.cpp ทิ้งทั้งก้อน (ตอบข้อความธรรมดา) · vLLM ตอบ 400
        if not any(key in wanted for key in ("json_schema", "schema", "parameters")):
            raise _bad(
                f"{_FIELD}.type", 'is required: "text", "json_object" or "json_schema".')
    elif not isinstance(kind, str):
        raise _bad(f"{_FIELD}.type", 'must be "text", "json_object" or "json_schema".')
    elif kind != "json_schema":
        return wanted, []

    out = dict(wanted)
    inner = out.get("json_schema")
    if inner is not None and not isinstance(inner, dict):
        raise _bad(_WRAPPER, f"must be an object: {_SHAPE}.")
    inner = dict(inner or {})
    adjusted: list[str] = []

    if kind is None:
        adjusted.append(f'{_FIELD}.type="json_schema"')
    for key in _INNER_KEYS:
        if key not in out:
            continue
        value = out.pop(key)
        if key in inner:
            adjusted.append(f"{_FIELD}.{key}->(dropped)")
        else:
            inner[key] = value
            adjusted.append(f"{_FIELD}.{key}->{_WRAPPER}.{key}")
    if "parameters" in inner:
        value = inner.pop("parameters")
        if "schema" in inner:
            adjusted.append(f"{_WRAPPER}.parameters->(dropped)")
        else:
            inner["schema"] = value
            adjusted.append(f"{_WRAPPER}.parameters->{_WRAPPER}.schema")

    schema = inner.get("schema")
    if schema is None:
        raise _bad(
            f"{_WRAPPER}.schema",
            f'is required when type is "json_schema"; expected {_SHAPE}. Without it the '
            'model is not held to any schema. For free-form JSON use {"type": "json_object"}.',
        )
    if not isinstance(schema, dict):
        raise _bad(f"{_WRAPPER}.schema", "must be a JSON Schema object.")
    name = inner.get("name")
    if name is None or name == "":
        inner["name"] = "response"
        adjusted.append(f'{_WRAPPER}.name="response"')
    elif not isinstance(name, str):
        raise _bad(f"{_WRAPPER}.name", "must be a string.")

    if not adjusted:
        return wanted, []
    out["type"] = "json_schema"
    out["json_schema"] = inner
    return out, adjusted
