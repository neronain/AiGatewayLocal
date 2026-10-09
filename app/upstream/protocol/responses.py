"""OpenAI Responses surface: translation to and from chat completions (Codex).

Codex speaks only the Responses API. Nearly every backend we run - vLLM, llama.cpp,
Ollama - speaks chat completions. So the gateway does for Codex exactly what it
already does for Claude Code: translate on the way out, translate back on the way
in, including the streaming event sequence.

The two shapes differ in more than field names:

    chat completions          Responses
    -----------------------   ---------------------------------------------
    messages[]                input[] - messages *and* tool traffic, mixed
    system message            instructions (a top-level string)
    max_tokens                max_output_tokens
    tools[].function.name     tools[].name          (flattened)
    choices[].message         output[] - one item per message or tool call
    usage.prompt_tokens       usage.input_tokens

The mixed `input` array is the part worth being careful about: a turn that used
tools comes back with `function_call` and `function_call_output` items sitting
beside the messages, not nested inside them. Dropping them would hand the model a
conversation where it asked for a tool and never learned the answer.

Reasoning crosses in one direction, as on the Anthropic surface. A reasoning
model's chain of thought (`reasoning_content` / `reasoning` on the backend)
comes back as a `reasoning` output item - raw text in `content[].reasoning_text`,
`summary` left empty because nothing here summarised it. Unlike Anthropic's
`thinking` blocks this is not opt-in: on the Responses API a reasoning model's
output carries reasoning items whether or not the caller asked, so clients
already have to step over them. `reasoning` items in the *request* are still
dropped (see `responses_to_openai_request`).

Nothing else in a request is dropped silently. A part chat completions cannot
express is reported by `untranslatable` and refused with a 400 naming it; a
hosted-tool definition is skipped and the caller told (`ignored_in_translation`).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.core.errors import ErrorCode, GatewayError
from app.upstream.protocol.reasoning import reasoning_text

__all__ = [
    "ResponsesStreamAdapter",
    "ignored_in_translation",
    "new_response_id",
    "openai_to_responses_response",
    "response_format",
    "responses_to_openai_request",
    "tool_output_parts",
    "untranslatable",
]


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _item_id(prefix: str = "msg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Responses request -> OpenAI chat completions
# ---------------------------------------------------------------------------
# กติกาของขาเข้าเหมือนตัวแปลของ Anthropic (app/upstream/protocol/anthropic.py): ไม่มีอะไรหาย
# เงียบ ๆ — ส่งต่อ/แปล · ปฏิเสธพร้อมบอกตำแหน่ง (`untranslatable`) · เพิกเฉยโดยตั้งใจ ·
# และอีกหนึ่งอย่างที่มีเฉพาะฝั่งนี้: **ข้ามแล้วบอก** (`ignored_in_translation`) สำหรับนิยาม
# เครื่องมือที่ OpenAI รันเอง ซึ่ง client บางตัวแนบมาทุกคำขอโดยผู้ใช้ไม่ได้สั่ง
#
# เคสที่ตรวจพบ 2026-10-06: `text.format` เป็น json_schema กับ tool ชนิด `custom` → backend ได้
# แค่ ['max_tokens', 'messages', 'model', 'tools'] · ไม่มี `response_format` (ทั้งที่ทาง chat
# ส่งให้) และ tool ชนิด custom หายไปจากรายการโดยไม่มีใครรู้

_TEXT_PARTS = frozenset({"input_text", "output_text", "text"})
# item ที่ตัวแปลรู้จัก · None กับ "message" คือข้อความ
_MESSAGE_ITEMS = (None, "message")
# เครื่องมือที่ client รันเอง · ชนิดอื่นทั้งหมดคือของที่ OpenAI รันให้ (web_search ·
# file_search · code_interpreter · image_generation · mcp · computer_use_preview …)
_FUNCTION_TOOL = (None, "function")

Problem = tuple[str, str]  # (ตำแหน่งในคำขอ, ทำไมแปลไม่ได้)


def _image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    image: dict[str, Any] = {"url": url}
    if part.get("detail"):
        image["detail"] = part["detail"]
    return {"type": "image_url", "image_url": image}


def _content_parts_to_openai(content: Any) -> list[dict] | str | None:
    """Responses content parts -> chat content. Returns a bare string when it can."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in _TEXT_PARTS:
            parts.append({"type": "text", "text": part.get("text") or ""})
        elif ptype == "refusal":
            # คำปฏิเสธที่โมเดลเคยตอบ — เป็นส่วนของประวัติ ถ้าหายไป โมเดลจะเห็นเทิร์นของ
            # ตัวเองว่างเปล่า
            parts.append({"type": "text", "text": part.get("refusal") or ""})
        elif ptype == "input_image" and (image := _image_part(part)):
            parts.append(image)

    if not parts:
        return None
    # Collapse a lone text part: some backends only accept the array form for
    # genuinely multimodal turns.
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def tool_output_parts(output: Any) -> tuple[str, list[dict[str, Any]]] | None:
    """`function_call_output.output` → (ข้อความของ tool message, รูปที่ตามไปใน user message)

    None = มีชิ้นส่วนที่แสดงเป็น chat ไม่ได้ · `output` เป็นสตริงก็ได้ เป็นรายการชิ้นส่วน
    (`input_text` / `input_image`) ก็ได้ — Codex คืนภาพที่ tool อ่านมาด้วยรูปหลัง · เดิมรายการ
    ถูก `json.dumps` ทั้งก้อน โมเดลจึงได้ base64 ของรูปเป็น *ข้อความ* หลายแสนอักขระ
    """
    if output is None:
        return "", []
    if isinstance(output, str):
        return output, []
    if not isinstance(output, list):
        return json.dumps(output, ensure_ascii=False), []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in output:
        if not isinstance(part, dict):
            return None
        ptype = part.get("type")
        if ptype in _TEXT_PARTS:
            texts.append(part.get("text") or "")
        elif ptype == "input_image" and (image := _image_part(part)):
            images.append(image)
        else:
            return None
    if images:
        count = len(images)
        texts.append(
            f"[{count} image{'s' if count != 1 else ''} returned by this tool - "
            "attached in the next message]"
        )
    return "\n".join(texts), images


def response_format(body: dict[str, Any]) -> dict[str, Any] | None:
    """`text.format` → `response_format` ของ chat completions · None = ข้อความธรรมดา"""
    text = body.get("text")
    wanted = text.get("format") if isinstance(text, dict) else None
    if not isinstance(wanted, dict):
        return None
    kind = wanted.get("type")
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema" and isinstance(wanted.get("schema"), dict):
        schema: dict[str, Any] = {
            "name": wanted.get("name") or "response", "schema": wanted["schema"]}
        for key in ("strict", "description"):
            if wanted.get(key) is not None:
                schema[key] = wanted[key]
        return {"type": "json_schema", "json_schema": schema}
    return None


_FORMAT_SHAPE = '{"type": "json_schema", "name": "...", "schema": {...}}'


def _format_problem(body: dict[str, Any]) -> Problem | None:
    """`text.format` ที่ขอรูปแบบคำตอบไว้ แต่ `response_format` ข้างบนแปลออกมาไม่ได้

    เดิมคืน None เฉย ๆ แล้วคำขอไปถึง backend โดยไม่มี `response_format` — ตรวจ 2026-10-09:
    ลืม `schema` · ใช้ `parameters` (ชื่อของ function tool) · ส่งรูปซ้อนของ chat
    (`json_schema: {...}`) ได้ 200 เป็นข้อความอิสระทั้งสามแบบ · อาการเดียวกับที่ llama.cpp ทำบน
    ทาง chat (app/core/responseformat.py) ต่างกันที่คนทิ้งคือตัวแปลนี้เอง
    """
    text = body.get("text")
    wanted = text.get("format") if isinstance(text, dict) else None
    # ไม่ได้ขอ · ก้อนว่าง · หรือแปลได้ — ไม่มีอะไรหาย
    if wanted is None or wanted == {} or response_format(body) is not None:
        return None
    if not isinstance(wanted, dict):
        return "text.format", f"must be an object such as {_FORMAT_SHAPE}"
    kind = wanted.get("type")
    if kind == "text":
        return None
    if kind == "json_schema":
        return (
            "text.format.schema",
            f"a json_schema format carries its schema here as an object: {_FORMAT_SHAPE}. "
            "Without it the model would not be held to any schema",
        )
    return (
        "text.format.type",
        (f"format type '{kind}' has" if kind is not None else "a format without a type has")
        + ' no chat-completions equivalent; use "text", "json_object" or "json_schema"',
    )


def ignored_in_translation(body: dict[str, Any]) -> list[str]:
    """นิยามเครื่องมือที่จะถูกข้ามเมื่อแปลเป็น chat completions — ผู้เรียกจะถูกบอกทาง header

    เครื่องมือที่ OpenAI รันให้เอง ไม่มีใครรันให้บน backend ในบ้าน · ที่ไม่ปฏิเสธทั้งคำขอ
    เพราะ client อย่าง Codex แนบ `web_search` มากับ *ทุก* คำขอโดยผู้ใช้ไม่ได้เลือก — ตอบ 400
    คือปิดประตูใส่ client ทั้งตัว · แต่ถ้าคำขอ *บังคับ* ให้ใช้เครื่องมือนั้น (`tool_choice`)
    หรือมีผลของมันอยู่ในประวัติ คำขอนั้นทำตามไม่ได้จริง ๆ และถูกปฏิเสธใน `untranslatable`
    """
    tools = body.get("tools")
    return [
        f"tools[{index}]:{tool.get('type')}"
        for index, tool in enumerate(tools if isinstance(tools, list) else [])
        if isinstance(tool, dict) and tool.get("type") not in (*_FUNCTION_TOOL, "custom")
    ]


def untranslatable(body: dict[str, Any]) -> list[Problem]:
    """ส่วนของคำขอที่ chat completions แสดงไม่ได้ — ว่าง = แปลได้ทั้งคำขอ

    ตรวจอย่างเดียว ไม่สร้าง payload (ดูคำอธิบายที่ตัวคู่ใน protocol/anthropic.py)
    """
    problems: list[Problem] = []

    tools = body.get("tools")
    for index, tool in enumerate(tools if isinstance(tools, list) else []):
        if isinstance(tool, dict) and tool.get("type") == "custom":
            problems.append((
                f"tools[{index}]",
                "custom (free-form) tools cannot be offered to a model served through "
                "chat-completions translation: its calls come back as function calls, "
                "not custom_tool_call items. Declare it as a function tool",
            ))

    choice = body.get("tool_choice")
    if isinstance(choice, dict) and not (
        choice.get("type") in _FUNCTION_TOOL and choice.get("name")
    ):
        problems.append((
            "tool_choice",
            f"tool_choice of type '{choice.get('type')}' has no chat-completions "
            "equivalent; use \"auto\", \"none\", \"required\" or name a function tool",
        ))

    for field, why in (
        ("conversation", "this gateway keeps no conversation state; send the full input"),
        ("prompt", "stored prompt templates live on OpenAI's servers, not on this gateway"),
    ):
        if body.get(field):
            problems.append((field, why))
    if body.get("background") is True:
        problems.append(("background", "background responses need server-side storage"))
    if (problem := _format_problem(body)) is not None:
        problems.append(problem)

    items = body.get("input")
    for index, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        path = f"input[{index}]"
        itype = item.get("type")
        if itype in ("function_call", "reasoning"):
            continue
        if itype == "function_call_output":
            if tool_output_parts(item.get("output")) is None:
                problems.append((
                    f"{path}.output",
                    "this tool output holds parts other than input_text and input_image, "
                    "which a chat-completions tool message cannot carry",
                ))
            continue
        if itype == "item_reference":
            problems.append((
                path,
                "item_reference points at an item stored on the server; this gateway "
                "keeps no conversation state. Send the item itself",
            ))
            continue
        if itype not in _MESSAGE_ITEMS:
            problems.append((
                path,
                f"input items of type '{itype}' have no chat-completions equivalent, so "
                "the model would answer without them. Remove them from the input",
            ))
            continue
        content = item.get("content")
        for p_index, part in enumerate(content if isinstance(content, list) else []):
            if isinstance(part, dict) and part.get("type") not in (
                *_TEXT_PARTS, "refusal", "input_image"
            ):
                problems.append((
                    f"{path}.content[{p_index}]",
                    f"content parts of type '{part.get('type')}' have no "
                    "chat-completions equivalent",
                ))
    return problems


# บทบาทของ Responses API ที่ chat completions ไม่มี
#
# `developer` คือชื่อใหม่ของ `system` ฝั่ง OpenAI (Codex ส่งคำสั่งเรื่อง sandbox/สิทธิ์มาใน
# บทบาทนี้ทุกคำขอ) · chat template ของโมเดลในบ้านรู้จักแค่ system/user/assistant/tool —
# บทบาทที่ไม่รู้จักถูกบาง template ปฏิเสธทั้งคำขอ และถูกบาง template ข้ามไปเงียบ ๆ ซึ่งแย่กว่า:
# โมเดลไม่เคยเห็นคำสั่งพวกนั้นเลยและไม่มี error ให้ใครรู้
_CHAT_ROLE = {"developer": "system"}


def _system_text(content: list[dict] | str) -> str | None:
    """ข้อความของ system message ที่รวมเข้าก้อนแรกได้ · None = มีอย่างอื่นนอกจากข้อความ

    ทำไมต้องรวม ไม่ใช่แค่เปลี่ยนชื่อบทบาท: `instructions` กลายเป็น system message ตัวแรกอยู่
    แล้ว ถ้า item `developer` ที่ตามมากลายเป็น system message *ตัวที่สอง* template ของ Qwen3
    รุ่นใหม่โยน "System message must be at the beginning." และของ Gemma/Mistral ที่บังคับสลับ
    user/assistant ก็ไม่รับ · system ก้อนเดียวที่ตำแหน่งแรกคือรูปเดียวที่ทุก template รับ
    """
    if isinstance(content, str):
        return content
    if all(part.get("type") == "text" for part in content):
        return "\n\n".join(part["text"] for part in content)
    return None


def responses_to_openai_request(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    # ด่านสุดท้าย · ทางปกติถูกปฏิเสธไปตั้งแต่ด่าน capability แล้ว (ดู `untranslatable`)
    if problems := untranslatable(body):
        path, why = problems[0]
        raise GatewayError(ErrorCode.INVALID_REQUEST, f"{path}: {why}.", param=path)

    messages: list[dict] = []

    # ข้อความระบบทั้งหมดที่มาก่อนบทสนทนา — `instructions` กับ item บทบาท system/developer
    # ที่อยู่หัว `input` — รวมเป็น system message **ก้อนเดียวที่ตำแหน่งแรก** (ดู _system_text)
    preamble: list[str] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        preamble.append(instructions)
    # ยังอยู่ในช่วงหัวของ input ไหม — จบทันทีที่เจอ item แรกที่เป็นบทสนทนาจริง
    in_preamble = True

    payload_input = body.get("input")
    if isinstance(payload_input, str):
        messages.append({"role": "user", "content": payload_input})
        items: list[Any] = []
    else:
        items = payload_input if isinstance(payload_input, list) else []

    # Consecutive function_call items belong to one assistant turn, the way chat
    # completions models them: one message carrying every tool_call it asked for.
    pending_calls: list[dict] = []

    def flush_calls() -> None:
        if pending_calls:
            messages.append(
                {"role": "assistant", "content": None, "tool_calls": list(pending_calls)}
            )
            pending_calls.clear()

    # รูปที่ tool คืนมา · tool message พกได้แต่ข้อความ รูปจึงไปใน user message ที่ตามหลัง —
    # **หลังผลของ tool ทั้งชุด** ไม่ใช่แทรกกลาง: chat completions ให้ tool message ของการเรียก
    # ชุดเดียวกันอยู่ติดกัน
    pending_images: list[dict] = []

    def flush_images() -> None:
        if pending_images:
            messages.append({"role": "user", "content": list(pending_images)})
            pending_images.clear()

    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype != "function_call_output":
            flush_images()

        if itype == "function_call":
            in_preamble = False
            pending_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or _item_id("call"),
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
            continue

        flush_calls()

        if itype == "function_call_output":
            in_preamble = False
            call_id = item.get("call_id") or ""
            text, images = tool_output_parts(item.get("output")) or ("", [])
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
            if images:
                pending_images.append(
                    {"type": "text", "text": f"[image returned by tool call {call_id}]"})
                pending_images.extend(images)
            continue

        if itype == "reasoning":
            # ไม่ส่งต่อ: เป็นร่องรอยความคิดของ *โมเดลอื่น* backend อ่านแล้วสับสนเปล่า ๆ
            continue

        role = _CHAT_ROLE.get(item.get("role") or "user", item.get("role") or "user")
        content = _content_parts_to_openai(item.get("content"))
        if content is None:
            continue
        if role == "system" and in_preamble and (text := _system_text(content)) is not None:
            preamble.append(text)
            continue
        in_preamble = False
        messages.append({"role": role, "content": content})

    flush_calls()
    flush_images()

    if preamble:
        messages.insert(0, {"role": "system", "content": "\n\n".join(preamble)})

    payload: dict[str, Any] = {"model": upstream_model, "messages": messages}
    if body.get("max_output_tokens") is not None:
        payload["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if body.get(key) is not None:
            payload[key] = body[key]

    if (wanted := response_format(body)) is not None:
        payload["response_format"] = wanted

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        # ชนิด custom ถูกปฏิเสธไปแล้วข้างบน · ชนิดที่ OpenAI รันเองถูกข้ามและผู้เรียกถูกบอก
        # (ดู ignored_in_translation)
        converted = [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {"type": "object"},
                },
            }
            for tool in tools
            if isinstance(tool, dict) and tool.get("type") in _FUNCTION_TOOL
        ]
        if converted:
            payload["tools"] = converted

    choice = body.get("tool_choice")
    if isinstance(choice, str):
        payload["tool_choice"] = choice
    elif isinstance(choice, dict) and choice.get("name"):
        payload["tool_choice"] = {
            "type": "function",
            "function": {"name": choice["name"]},
        }

    return payload


# ---------------------------------------------------------------------------
# OpenAI response -> Responses response
# ---------------------------------------------------------------------------
_STATUS_FOR_FINISH = {
    "stop": "completed",
    "tool_calls": "completed",
    "function_call": "completed",
    "length": "incomplete",
    "content_filter": "incomplete",
}

# ทำไมถึงไม่จบ — สองเหตุผลนี้ client แก้คนละทาง (เพิ่มงบ output กับเปลี่ยนคำขอ)
# เดิมรายงาน "max_output_tokens" ทั้งคู่ คำตอบที่ถูกกรองจึงดูเหมือนงบไม่พอ
_INCOMPLETE_REASON = {"length": "max_output_tokens", "content_filter": "content_filter"}


def _incomplete_details(finish: str) -> dict[str, str] | None:
    if _STATUS_FOR_FINISH.get(finish, "completed") != "incomplete":
        return None
    return {"reason": _INCOMPLETE_REASON.get(finish, "max_output_tokens")}


def _reasoning_item(item_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    """item ความคิดของโมเดล ในรูปที่ Responses API ใช้กับความคิดดิบ

    `summary` ว่างแต่ต้องมี: เราไม่ได้สรุปอะไร และ Codex ประกาศฟิลด์นี้เป็นบังคับ ·
    ไม่ใส่ `encrypted_content`: ของ OpenAI ใช้ส่งความคิดกลับเข้ามาในรอบถัดไป เราไม่มีให้
    และไม่ปลอมขึ้นมา (ขาเข้าทิ้ง item ชนิดนี้อยู่แล้ว)
    """
    return {
        "id": item_id,
        "type": "reasoning",
        "status": status,
        "summary": [],
        "content": [{"type": "reasoning_text", "text": text}] if text else [],
    }


def _message_item(item_id: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _usage_block(usage: Any) -> dict[str, Any]:
    usage = usage if isinstance(usage, dict) else {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": int(usage.get("total_tokens") or prompt + completion),
    }


def openai_to_responses_response(
    payload: dict[str, Any], model_alias: str, response_id: str | None = None
) -> dict[str, Any]:
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    finish = choice.get("finish_reason") or "stop"

    output: list[dict] = []
    # ความคิดมาก่อนคำตอบเสมอ · เดิมถูกทิ้ง โมเดลที่ใช้งบหมดไปกับการคิดจึงคืน `output: []`
    # กับ status "incomplete" — คิดเงินเต็มโดยไม่มีอะไรให้ดูเลยว่าทำอะไรไป
    thought = reasoning_text(message)
    if thought:
        output.append(_reasoning_item(_item_id("rs"), thought))
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(_message_item(_item_id(), text))

    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        output.append(
            {
                "id": _item_id("fc"),
                "type": "function_call",
                "status": "completed",
                "call_id": call.get("id") or _item_id("call"),
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "{}",
            }
        )

    status = _STATUS_FOR_FINISH.get(finish, "completed")
    return {
        "id": response_id or new_response_id(),
        "object": "response",
        "created_at": int(payload.get("created") or time.time()),
        "status": status,
        "model": model_alias,
        "output": output,
        "output_text": text if isinstance(text, str) else "",
        "parallel_tool_calls": True,
        "usage": _usage_block(payload.get("usage")),
        "error": None,
        "incomplete_details": _incomplete_details(finish),
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# OpenAI SSE chunks -> Responses events
# ---------------------------------------------------------------------------
class ResponsesStreamAdapter:
    """Convert an OpenAI chunk stream into the Responses event sequence.

    Codex reads the typed events, not a raw text stream, and it counts on the
    open/close pairs being balanced:

        response.created
        response.output_item.added                       (reasoning)
        response.reasoning_text.delta* / .done
        response.output_item.done
        response.output_item.added / response.content_part.added   (message)
        response.output_text.delta* / .done
        response.content_part.done / response.output_item.done
        response.output_item.added                       (function_call)
        response.function_call_arguments.delta* / .done
        response.output_item.done
        response.completed | response.incomplete

    Items are opened lazily - an OpenAI stream does not announce boundaries, it
    just starts sending a different kind of delta - and each one takes the next
    `output_index` when it opens and keeps it until it closes.

    Every event carries a `sequence_number`; the client uses it to detect a gap,
    so it has to increase by one across *all* event types, not per type.
    """

    def __init__(self, model_alias: str) -> None:
        self.model_alias = model_alias
        self.response_id = new_response_id()
        self._seq = 0
        self._started = False
        # item ความคิด/ข้อความที่เปิดค้างอยู่ — เปิดได้ทีละอัน
        # {"kind": "reasoning" | "text", "id", "index", "text"}
        self._open: dict[str, Any] | None = None
        # item ที่ปิดแล้ว ตามช่อง · ตอนจบใช้ชุดนี้ประกอบ `output` — ต้องเป็น item ตัวเดียวกับ
        # ที่ stream ไปแล้ว (id เดิม) ไม่ใช่สร้างใหม่ให้ client เห็นของสองชุด
        self._closed: dict[int, dict[str, Any]] = {}
        self._text = ""
        self._output_index = 0  # ช่องว่างถัดไป
        # openai tool_call index -> {"item_id", "call_id", "name", "args", "output_index"}
        self._tools: dict[int, dict[str, Any]] = {}
        self._finish: str | None = None
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    # -- helpers ------------------------------------------------------------
    def _next(self, event_type: str, payload: dict[str, Any]) -> tuple[str, dict]:
        payload = {"type": event_type, "sequence_number": self._seq, **payload}
        self._seq += 1
        return event_type, payload

    def _skeleton(self, status: str) -> dict[str, Any]:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.model_alias,
            "output": [],
            "parallel_tool_calls": True,
            "error": None,
            "incomplete_details": None,
            "metadata": {},
        }

    def _take_index(self) -> int:
        index, self._output_index = self._output_index, self._output_index + 1
        return index

    # -- stream -------------------------------------------------------------
    def start_events(self) -> list[tuple[str, dict]]:
        if self._started:
            return []
        self._started = True
        return [
            self._next("response.created", {"response": self._skeleton("in_progress")}),
            self._next("response.in_progress", {"response": self._skeleton("in_progress")}),
        ]

    def handle_chunk(self, chunk: dict[str, Any]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = list(self.start_events())

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage["input_tokens"] = int(
                usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            )
            self.usage["output_tokens"] = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )

        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return events
        choice = choices[0]
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self._finish = choice["finish_reason"]

        # ส่งความคิดออกไป *ตอนที่มันเกิด* · เดิมทิ้ง stream จึงเงียบสนิทตลอดช่วงคิด ซึ่ง
        # client ที่มี idle timeout (Codex: 5 นาที) อ่านว่าสายหลุด ทั้งที่โมเดลยังทำงานอยู่
        thought = reasoning_text(delta)
        if thought:
            events.extend(self._append("reasoning", thought))

        text = delta.get("content")
        if isinstance(text, str) and text:
            events.extend(self._append("text", text))

        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict):
                events.extend(self._handle_tool_call(call))

        return events

    def _append(self, kind: str, piece: str) -> list[tuple[str, dict]]:
        """ต่อ `piece` เข้า item ชนิด `kind` — เปิดตัวใหม่ถ้าตัวที่ค้างอยู่เป็นคนละชนิด"""
        events: list[tuple[str, dict]] = []
        if self._open is None or self._open["kind"] != kind:
            events.extend(self._close_open())
            events.extend(self._open_item(kind))
        item = self._open
        item["text"] += piece
        if kind == "text":
            self._text += piece
        events.append(
            self._next(
                "response.output_text.delta" if kind == "text"
                else "response.reasoning_text.delta",
                {
                    "item_id": item["id"],
                    "output_index": item["index"],
                    "content_index": 0,
                    "delta": piece,
                },
            )
        )
        return events

    def _open_item(self, kind: str) -> list[tuple[str, dict]]:
        index = self._take_index()
        if kind == "reasoning":
            item_id = _item_id("rs")
            self._open = {"kind": kind, "id": item_id, "index": index, "text": ""}
            return [
                self._next(
                    "response.output_item.added",
                    {"output_index": index, "item": _reasoning_item(item_id, "", "in_progress")},
                )
            ]

        item_id = _item_id()
        self._open = {"kind": kind, "id": item_id, "index": index, "text": ""}
        return [
            self._next(
                "response.output_item.added",
                {
                    "output_index": index,
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            self._next(
                "response.content_part.added",
                {
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def _handle_tool_call(self, call: dict[str, Any]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        index = int(call.get("index") or 0)
        fn = call.get("function") or {}

        state = self._tools.get(index)
        if state is None:
            # A text or reasoning item, if any, is closed before a tool item
            # opens: two items must never be open at once, and each has its own
            # output_index (text and the first tool call used to share index 0).
            events.extend(self._close_open())
            state = {
                "item_id": _item_id("fc"),
                "call_id": call.get("id") or _item_id("call"),
                "name": fn.get("name") or "",
                "args": "",
                "output_index": self._take_index(),
            }
            self._tools[index] = state
            events.append(
                self._next(
                    "response.output_item.added",
                    {
                        "output_index": state["output_index"],
                        "item": {
                            "id": state["item_id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": state["call_id"],
                            "name": state["name"],
                            "arguments": "",
                        },
                    },
                )
            )

        if fn.get("name") and not state["name"]:
            state["name"] = fn["name"]

        arguments = fn.get("arguments")
        if isinstance(arguments, str) and arguments:
            state["args"] += arguments
            events.append(
                self._next(
                    "response.function_call_arguments.delta",
                    {
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "delta": arguments,
                    },
                )
            )
        return events

    def _close_open(self) -> list[tuple[str, dict]]:
        """ปิด item ความคิด/ข้อความที่ค้างอยู่ ที่ช่องของมันเอง"""
        if self._open is None:
            return []
        open_item, self._open = self._open, None
        item_id, index, text = open_item["id"], open_item["index"], open_item["text"]
        where = {"item_id": item_id, "output_index": index, "content_index": 0}

        if open_item["kind"] == "reasoning":
            item = _reasoning_item(item_id, text)
            self._closed[index] = item
            return [
                self._next("response.reasoning_text.done", {**where, "text": text}),
                self._next("response.output_item.done", {"output_index": index, "item": item}),
            ]

        item = _message_item(item_id, text)
        self._closed[index] = item
        return [
            self._next("response.output_text.done", {**where, "text": text}),
            self._next(
                "response.content_part.done",
                {**where, "part": {"type": "output_text", "text": text, "annotations": []}},
            ),
            self._next("response.output_item.done", {"output_index": index, "item": item}),
        ]

    def finish_events(
        self, *, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> list[tuple[str, dict]]:
        """`input_tokens` / `output_tokens` = ตัวเลขที่เกตเวย์บันทึกลงแถว usage

        ผู้เรียกต้องเห็นชุดเดียวกับที่ถูกคิด — backend ที่ไม่รายงาน usage เคยทำให้
        `response.completed.usage` เป็น 0 ทั้งหมด ทั้งที่แถว usage มีค่าประมาณ
        """
        events = list(self.start_events())
        events.extend(self._close_open())
        self._settle_usage(input_tokens, output_tokens)

        for _, state in sorted(self._tools.items()):
            events.append(
                self._next(
                    "response.function_call_arguments.done",
                    {
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "arguments": state["args"],
                    },
                )
            )
            item = {
                "id": state["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": state["args"],
            }
            events.append(
                self._next(
                    "response.output_item.done",
                    {"output_index": state["output_index"], "item": item},
                )
            )
            self._closed[state["output_index"]] = item

        finish = self._finish or "stop"
        status = _STATUS_FOR_FINISH.get(finish, "completed")
        final = self._skeleton(status)
        final["output"] = [self._closed[index] for index in sorted(self._closed)]
        final["output_text"] = self._text
        final["usage"] = _usage_block(self.usage)
        final["incomplete_details"] = _incomplete_details(finish)
        events.append(
            self._next(
                "response.completed" if status == "completed" else "response.incomplete",
                {"response": final},
            )
        )
        return events

    def _settle_usage(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if input_tokens is not None:
            self.usage["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            self.usage["output_tokens"] = int(output_tokens)
        if input_tokens is not None or output_tokens is not None:
            # total ของ backend (ถ้ามี) ไม่ตรงกับตัวเลขที่เพิ่งตั้ง — ให้ _usage_block บวกใหม่
            self.usage.pop("total_tokens", None)

    def resume(self, response_id: str | None, next_sequence: int) -> None:
        """ต่อจาก stream ที่ backend พูด Responses เอง — ใช้ id และลำดับของมัน

        ทางที่ไม่ได้แปลไม่มี adapter เดินตาม แต่ถ้าสายขาดกลางทางก็ยังต้องปิดด้วย
        `response.failed` ที่มี id เดิมและ sequence_number ต่อจากตัวสุดท้ายที่ส่งไป
        """
        if response_id:
            self.response_id = response_id
        self._seq = max(self._seq, next_sequence)
        self._started = True

    def fail_events(
        self,
        code: str,
        message: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> list[tuple[str, dict]]:
        """ปิด stream ด้วย `response.failed` — event ปิดท้ายของ Responses เมื่อคำตอบไม่จบ

        เดิมสายไป backend ขาดกลางทางแล้ว stream หยุดหลัง `response.output_text.delta` เฉย ๆ:
        ไม่มี `response.completed` ไม่มี `response.failed` · Codex อ่านสายที่ปิดโดยไม่มี event
        ปิดท้ายว่า "stream closed before response.completed" ซึ่งไม่บอกอะไรเลยว่าเกิดอะไรขึ้น

        item ที่ส่งไปแล้วครึ่งทางไม่ถูกปิดทีละตัว: ผู้อ่านทิ้งทั้ง response เมื่อเห็น failed
        """
        events = list(self.start_events())
        self._settle_usage(input_tokens, output_tokens)
        final = self._skeleton("failed")
        final["output"] = [self._closed[index] for index in sorted(self._closed)]
        final["error"] = {"code": code, "message": message}
        final["usage"] = _usage_block(self.usage)
        events.append(self._next("response.failed", {"response": final}))
        return events
