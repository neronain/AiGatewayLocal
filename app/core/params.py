"""ตรวจชนิดและช่วงของพารามิเตอร์ในคำขอ — ก่อนด่านอื่นทุกด่าน

ทำไมต้องมีไฟล์นี้
-----------------
เกตเวย์ *ใช้* ค่าพวกนี้เองก่อนจะส่งต่อ: เทียบ `max_tokens` กับเพดานของโมเดล · อ่าน
`stream_options.include_usage` · ดู `stream` ว่าจะเปิด SSE ไหม · โค้ดพวกนั้นเขียนโดยเชื่อว่า
ค่ามาถูกชนิด ผลที่ตรวจพบ 2026-10-06:

    max_tokens: "100" / [1]   TypeError กลางด่าน context   -> 500
    max_tokens: -5            ถูกส่งให้ backend เป็น 1 โดยไม่บอกใคร
    max_tokens: 0             ถูกมองว่า "ไม่ได้ระบุ" แล้วได้เพดานเต็มของโมเดล
    stream_options: "yes"     ตอบ 200 เปิดสตรีมแล้วค่อยพังข้างใน — client ได้สตรีมขาด
                              และแถว usage บันทึกว่า "success" 0 token

ทั้งหมดเป็นคำขอที่ผิดตั้งแต่ต้น คำตอบที่ถูกคือ 400 ที่บอกว่าฟิลด์ไหนผิด **ก่อน** จองช่อง
ก่อนตรวจโควตา และก่อนมีแถว usage — ผู้เรียกจึงเรียกตัวตรวจของ surface ตัวเองเป็นอย่างแรก
หลังอ่าน body

ขอบเขต: เฉพาะฟิลด์ที่เกตเวย์อ่านเองหรือแปลเอง · ฟิลด์อื่น (`frequency_penalty`, `logit_bias`,
…) ผ่านไปให้ backend ตัดสินเหมือนเดิม — backend ตอบ 400 ของมันเองได้ และเราไม่ต้องตามสเปก
ของทุกเซิร์ฟเวอร์ · ข้อยกเว้นคือ `response_format` ของ chat: backend ในบ้าน *ไม่* ตอบ 400 กับ
รูปที่ผิด มันตอบ 200 โดยไม่ใช้ schema (วัด 2026-10-09 — ดู app/core/responseformat.py)

ค่าที่ถูกแต่มาในรูปที่ไม่ตรงเป๊ะถูกปรับให้เข้ารูปใน body เลย (`max_tokens: 100.0` -> `100`):
JSON ไม่แยก int กับ float และ client บางตัวส่งเลขจำนวนเต็มเป็นทศนิยม · ด่านถัดไปจึงเห็น
int เสมอโดยไม่ต้องแปลงเองทุกจุด
"""

from __future__ import annotations

from typing import Any

from app.core import notices, responseformat
from app.core.errors import ErrorCode, GatewayError

# เท่ากับเพดานที่ OpenAI ระบุไว้สำหรับ `n` · เกินนี้ไม่ใช่การใช้งาน เป็นการพิมพ์ผิด
MAX_CHOICES = 128


def _bad(param: str, problem: str) -> GatewayError:
    return GatewayError(ErrorCode.INVALID_REQUEST, f"'{param}' {problem}", param=param)


def _is_number(value: Any) -> bool:
    # bool เป็นลูกของ int ใน Python — `temperature: true` ไม่ใช่ตัวเลขของใคร
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_int(body: dict[str, Any], param: str) -> int | None:
    """จำนวนเต็ม >= 1 หรือไม่ได้ระบุ · ค่าที่ผ่านถูกเขียนกลับเป็น int ใน body"""
    value = body.get(param)
    if value is None:
        return None
    # is_integer() เป็นเท็จกับ NaN/Infinity ด้วย — json ของ Python รับสองตัวนี้เข้ามาได้
    if not _is_number(value) or (isinstance(value, float) and not value.is_integer()):
        raise _bad(param, f"must be a positive integer, not {_describe(value)}.")
    if value < 1:
        raise _bad(param, f"must be at least 1 (got {int(value)}).")
    body[param] = int(value)
    return body[param]


def _number(body: dict[str, Any], param: str, low: float, high: float) -> None:
    value = body.get(param)
    if value is None:
        return
    if not _is_number(value):
        raise _bad(param, f"must be a number, not {_describe(value)}.")
    # NaN ตกทุกการเทียบ จึงเขียนเป็น "ไม่อยู่ในช่วง" ไม่ใช่ "น้อยกว่าหรือมากกว่า"
    if not low <= value <= high:
        raise _bad(param, f"must be between {low:g} and {high:g} (got {value}).")


def _boolean(body: dict[str, Any], param: str) -> None:
    value = body.get(param)
    if value is not None and not isinstance(value, bool):
        raise _bad(param, f"must be true or false, not {_describe(value)}.")


def _strings(body: dict[str, Any], param: str, *, allow_single: bool) -> None:
    value = body.get(param)
    if value is None or (allow_single and isinstance(value, str)):
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        shape = "a string or an array of strings" if allow_single else "an array of strings"
        raise _bad(param, f"must be {shape}.")


def _objects(body: dict[str, Any], param: str) -> list[dict[str, Any]]:
    value = body.get(param)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad(param, f"must be an array, not {_describe(value)}.")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _bad(f"{param}[{index}]", f"must be an object, not {_describe(item)}.")
    return value


def _named(holder: dict[str, Any], param: str) -> None:
    name = holder.get("name")
    if not isinstance(name, str) or not name:
        raise _bad(param, "must be a non-empty string.")


def _describe(value: Any) -> str:
    """ชนิดของค่าที่ส่งมา ในคำของ JSON — ไม่พิมพ์ตัวค่า (อาจเป็นเนื้อหาของผู้ใช้)"""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (int, float)):
        return "a non-integer number" if isinstance(value, float) else "a number"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return "null"


def _stream_options(body: dict[str, Any]) -> None:
    options = body.get("stream_options")
    if options is None:
        return
    if not isinstance(options, dict):
        raise _bad("stream_options", f"must be an object, not {_describe(options)}.")
    include = options.get("include_usage")
    if include is not None and not isinstance(include, bool):
        raise _bad("stream_options.include_usage", "must be true or false.")


def _sampling(body: dict[str, Any]) -> None:
    # ช่วงกว้างสุดที่ backend ตัวใดตัวหนึ่งรับ — แคบกว่านี้ให้ backend ตัดสินเอง
    _number(body, "temperature", 0.0, 2.0)
    _number(body, "top_p", 0.0, 1.0)


def _response_format(body: dict[str, Any]) -> None:
    """รูปของ structured output — ซ่อมใน body เลย แล้วบอกผู้เรียกว่าแก้อะไร

    ทำที่ด่านนี้เพราะทุกอย่างถัดไป (แคชคำตอบ · payload ที่ส่งจริง) อ่านจาก body ก้อนเดียวกัน
    จึงเห็นรูปที่แก้แล้วเหมือนกันหมด · null = ไม่ได้ขอ ส่งต่อเหมือนเดิม
    """
    wanted = body.get("response_format")
    if wanted is None:
        return
    body["response_format"], adjusted = responseformat.normalize(wanted)
    if adjusted:
        notices.put(notices.ADJUSTED, ", ".join(adjusted))


def validate_chat_params(body: dict[str, Any]) -> None:
    """/v1/chat/completions"""
    _positive_int(body, "max_tokens")
    _positive_int(body, "max_completion_tokens")
    choices = _positive_int(body, "n")
    if choices is not None and choices > MAX_CHOICES:
        raise _bad("n", f"must be at most {MAX_CHOICES} (got {choices}).")
    _sampling(body)
    _boolean(body, "stream")
    _stream_options(body)
    _strings(body, "stop", allow_single=True)
    for index, tool in enumerate(_objects(body, "tools")):
        # ชนิดอื่นนอกจาก function ส่งต่อไปให้ backend ตัดสิน — surface นี้ไม่ได้แปลอะไร
        if tool.get("type", "function") == "function":
            function = tool.get("function")
            if not isinstance(function, dict):
                raise _bad(f"tools[{index}].function", "must be an object.")
            _named(function, f"tools[{index}].function.name")
    choice = body.get("tool_choice")
    if choice is not None and not isinstance(choice, (str, dict)):
        raise _bad("tool_choice", f"must be a string or an object, not {_describe(choice)}.")
    _response_format(body)


def validate_messages_params(body: dict[str, Any]) -> None:
    """/v1/messages (Anthropic)"""
    _positive_int(body, "max_tokens")
    _sampling(body)
    top_k = body.get("top_k")
    if top_k is not None and (
        not _is_number(top_k)
        or (isinstance(top_k, float) and not top_k.is_integer())
        or top_k < 0
    ):
        raise _bad("top_k", "must be a non-negative integer.")
    _boolean(body, "stream")
    _strings(body, "stop_sequences", allow_single=False)
    for index, tool in enumerate(_objects(body, "tools")):
        _named(tool, f"tools[{index}].name")
    choice = body.get("tool_choice")
    if choice is not None:
        if not isinstance(choice, dict):
            raise _bad(
                "tool_choice",
                f"must be an object such as {{\"type\": \"auto\"}}, not {_describe(choice)}.",
            )
        if not isinstance(choice.get("type"), str):
            raise _bad("tool_choice.type", "must be a string.")


def validate_responses_params(body: dict[str, Any]) -> None:
    """/v1/responses"""
    _positive_int(body, "max_output_tokens")
    _sampling(body)
    _boolean(body, "stream")
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise _bad("instructions", f"must be a string, not {_describe(instructions)}.")
    for index, tool in enumerate(_objects(body, "tools")):
        # เครื่องมือที่ผู้ให้บริการรันเอง (web_search ฯลฯ) มีแค่ `type` · ที่ต้องมีชื่อคือ
        # เครื่องมือที่ client รันเอง
        if tool.get("type", "function") in ("function", "custom"):
            _named(tool, f"tools[{index}].name")
    choice = body.get("tool_choice")
    if choice is not None and not isinstance(choice, (str, dict)):
        raise _bad("tool_choice", f"must be a string or an object, not {_describe(choice)}.")
