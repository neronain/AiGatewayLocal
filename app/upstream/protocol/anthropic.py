"""Anthropic <-> OpenAI protocol translation (PRD §8, FR-25).

Claude Code speaks the Anthropic Messages API. Most local serving stacks (vLLM,
Ollama, SGLang) speak the OpenAI Chat Completions API. When a model's endpoint
declares `protocols.anthropic: true` the gateway forwards natively; otherwise it
translates here, in both directions, including the streaming event sequence.

Scope of the translation: text, images, text documents, system prompts, tool
definitions, tool_use / tool_result (including images a tool returned),
structured output, stop reasons, usage. Nothing in a request is dropped
silently: a part chat completions cannot express - a PDF document, a tool that
only Anthropic's API can run - is reported by `untranslatable` and refused with
a 400 naming it, before anything is forwarded. Hints that do not change what the
model is asked (prompt caching, metadata, service tier) are ignored on purpose
and never fabricated on the way back.

Reasoning is the one thing that crosses in a single direction. A reasoning model
behind an OpenAI backend returns its chain of thought in `reasoning_content`;
when the caller asked for thinking it comes back as Anthropic `thinking` blocks
(see `wants_thinking`). `thinking` blocks in the *request* are still dropped -
there is nowhere to put them in a chat completion.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.errors import ErrorCode, GatewayError
from app.upstream.protocol.reasoning import reasoning_text as _reasoning_text

# Anthropic finish reasons keyed by the OpenAI reason that produced them.
_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def wants_thinking(body: dict[str, Any]) -> bool:
    """ผู้เรียกขอ thinking มาหรือเปล่า — ตัวตัดสินว่าจะส่ง block `thinking` กลับไปไหม

    API ของ Anthropic คืน block `thinking` เฉพาะเมื่อคำขอเปิด thinking ไว้ · โค้ดฝั่ง client
    จำนวนมากเขียนโดยอาศัยข้อนี้ (`message.content[0].text`) ส่ง block ที่เขาไม่ได้ขอไปเป็น
    ตัวแรกคือทำให้โค้ดที่ถูกต้องพัง · Claude Code ส่ง `thinking` มาเองเมื่อเปิดใช้
    """
    thinking = body.get("thinking")
    return isinstance(thinking, dict) and thinking.get("type") not in (None, "disabled")


def _thinking_block(text: str) -> dict[str, Any]:
    # `signature` ว่าง: ลายเซ็นเป็นของ Anthropic ใช้ยืนยันว่า block มาจากโมเดลของเขาเอง
    # โมเดลในบ้านไม่มีให้ และเราไม่ปลอมขึ้นมา · ฟิลด์ยังต้องอยู่เพราะ SDK ประกาศว่าบังคับ
    # ตอน client ส่งประวัติกลับมา block นี้ถูกทิ้งที่ขาเข้าอยู่แล้ว (_content_blocks_to_openai)
    return {"type": "thinking", "thinking": text, "signature": ""}


# ---------------------------------------------------------------------------
# Anthropic request -> OpenAI request
# ---------------------------------------------------------------------------
# กติกาของขาเข้า: **ไม่มีอะไรหายเงียบ ๆ** · ทุกส่วนของคำขอเป็นหนึ่งในสี่อย่าง
#
#   ส่งต่อ / แปล   chat completions แสดงมันได้
#   ปฏิเสธ         แสดงไม่ได้ และเนื้อหานั้นคือสิ่งที่ผู้ใช้ส่งมาให้โมเดลอ่านหรือใช้
#                  → `untranslatable` รายงาน ด่าน capability ตอบ 400 ที่ระบุตำแหน่ง
#                  (เครื่องที่พูด Anthropic เองยังรับได้ — ดู capability.validate_model_capabilities)
#   เพิกเฉยโดยตั้งใจ  ไม่เปลี่ยนสิ่งที่โมเดลถูกถาม (cache_control · metadata · service_tier …)
#
# เคสที่ทำให้ต้องเขียนกติกานี้ (ตรวจ 2026-10-06): `tool_result` ที่มีรูป + บล็อก `document`
# → backend ได้ `{"role":"tool","tool_call_id":"toolu_1","content":""}` · ไบต์ของรูปกับข้อความ
# ในเอกสารไม่เคยไปถึง แต่เกตเวย์ยังบังคับ vision และบันทึก visual_input_tokens = 50

# เครื่องมือที่ผู้เรียกนิยามเอง (มี input_schema) · ชนิดอื่นคือของที่ API ของ Anthropic รันเอง
# (web_search_…) หรือรู้ schema เอง (bash_… · text_editor_… · computer_…) — โมเดลในบ้านไม่มีทั้งคู่
_CLIENT_TOOL_TYPES = (None, "custom")
_TOOL_CHOICES = ("auto", "any", "tool", "none")

Problem = tuple[str, str]  # (ตำแหน่งในคำขอ, ทำไมแปลไม่ได้)


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        media_type = source.get("media_type", "image/png")
        url = f"data:{media_type};base64,{source.get('data', '')}"
    else:
        url = source.get("url", "")
    return {"type": "image_url", "image_url": {"url": url}} if url else None


def document_parts(block: dict[str, Any]) -> list[dict[str, Any]] | None:
    """บล็อก `document` เป็นชิ้นส่วนของ chat · None = เป็นเอกสารที่แสดงเป็น chat ไม่ได้

    แปลได้: `source.type` = `text` (ข้อความล้วน) และ `content` (ข้อความ/รูปที่ผู้เรียกแตกมาแล้ว)
    แปลไม่ได้: `base64` · `url` · `file` — PDF ที่ต้องมีคนแตกหน้าออกมา ซึ่งเกตเวย์ไม่ทำ (PRD §13)

    `title` กับ `context` คือสิ่งที่ผู้เรียกบอกโมเดลเกี่ยวกับเอกสาร จึงไปด้วยเป็นบรรทัดนำ
    """
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    parts: list[dict[str, Any]] = []
    kind = source.get("type")
    if kind == "text" and isinstance(source.get("data"), str):
        parts.append(_text_part(source["data"]))
    elif kind == "content":
        inner = source.get("content")
        if isinstance(inner, str):
            parts.append(_text_part(inner))
        elif isinstance(inner, list):
            for item in inner:
                if not isinstance(item, dict):
                    return None
                if item.get("type") == "text":
                    parts.append(_text_part(item.get("text") or ""))
                elif item.get("type") == "image" and (image := _image_part(item)):
                    parts.append(image)
                else:
                    return None
        else:
            return None
    else:
        return None
    label = " - ".join(
        str(block[key]) for key in ("title", "context") if isinstance(block.get(key), str)
        and block[key]
    )
    if label:
        parts.insert(0, _text_part(f"[document: {label}]"))
    return parts


def tool_result_parts(block: dict[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
    """(ข้อความของ tool message, รูปที่ต้องตามไปใน user message ถัดไป) · None = แปลไม่ได้

    chat completions ให้ `role: "tool"` พกได้แต่ข้อความ · รูปที่ tool คืนมา (ภาพหน้าจอ · ไฟล์
    ภาพที่ Read) จึงไปใน user message ที่ตามหลังทันที ซึ่งเป็นท่าที่ตัวเชื่อม Anthropic→OpenAI
    ใช้กัน · ข้อความของ tool message บอกไว้ว่ามีรูปตามมา โมเดลจะได้โยงสองอย่างเข้าหากัน
    """
    inner = block.get("content")
    if inner is None:
        return "", []
    if isinstance(inner, str):
        return inner, []
    if not isinstance(inner, list):
        return json.dumps(inner, ensure_ascii=False), []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for item in inner:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "text":
            texts.append(item.get("text") or "")
        elif kind == "image":
            if image := _image_part(item):
                images.append(image)
        elif kind == "document":
            parts = document_parts(item)
            if parts is None:
                return None
            for part in parts:
                if part["type"] == "text":
                    texts.append(part["text"])
                else:
                    images.append(part)
        else:
            return None
    if images:
        count = len(images)
        texts.append(
            f"[{count} image{'s' if count != 1 else ''} returned by this tool - "
            "attached in the next message]"
        )
    return "\n".join(texts), images


def untranslatable(body: dict[str, Any]) -> list[Problem]:
    """ส่วนของคำขอที่ chat completions แสดงไม่ได้ — ว่าง = แปลได้ทั้งคำขอ

    ตรวจอย่างเดียว ไม่สร้าง payload: ถูกเรียกตอนอ่านรูปร่างคำขอ (ก่อนโควตา ก่อนเปิดสตรีม)
    เพื่อให้คำตอบเป็น 400 ทั้งก้อน ไม่ใช่สตรีมที่เปิดแล้วพังตอนแปล · `anthropic_to_openai_request`
    เรียกซ้ำเป็นด่านสุดท้าย และใช้ตัวช่วยชุดเดียวกัน (`document_parts` · `tool_result_parts`)
    สองที่จึงตัดสินไม่ตรงกันไม่ได้
    """
    problems: list[Problem] = []

    tools = body.get("tools")
    for index, tool in enumerate(tools if isinstance(tools, list) else []):
        if isinstance(tool, dict) and tool.get("type") not in _CLIENT_TOOL_TYPES:
            problems.append((
                f"tools[{index}]",
                f"tool type '{tool.get('type')}' is run or defined by Anthropic's own API; "
                "a model served through chat-completions translation cannot use it. "
                "Send it as a custom tool with an input_schema, or leave it out",
            ))

    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") not in _TOOL_CHOICES:
        problems.append((
            "tool_choice",
            f"tool_choice type '{choice.get('type')}' has no chat-completions equivalent",
        ))

    if body.get("mcp_servers"):
        problems.append((
            "mcp_servers",
            "remote MCP servers are connected by Anthropic's API, not by this gateway",
        ))
    if (problem := _format_problem(body)) is not None:
        problems.append(problem)

    messages = body.get("messages")
    for m_index, message in enumerate(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        assistant = message.get("role") == "assistant"
        for b_index, block in enumerate(message["content"]):
            if not isinstance(block, dict):
                continue
            path = f"messages[{m_index}].content[{b_index}]"
            kind = block.get("type")
            if kind == "document" and document_parts(block) is None:
                problems.append((path, _DOCUMENT_PROBLEM))
            elif kind == "tool_result":
                if assistant:
                    problems.append((path, "a tool_result block belongs in a user message"))
                elif tool_result_parts(block) is None:
                    problems.append((
                        f"{path}.content",
                        "this tool_result holds content other than text, images and text "
                        "documents, which a chat-completions tool message cannot carry",
                    ))
            elif kind == "tool_use" and not assistant:
                problems.append((path, "a tool_use block belongs in an assistant message"))
    return problems


_DOCUMENT_PROBLEM = (
    "only documents with a 'text' or 'content' source can be sent to a model served "
    "through chat-completions translation; PDF, URL and file sources need Anthropic's own "
    "document handling. Extract the text and send that instead"
)


def _collapse(parts: list[dict[str, Any]]) -> Any:
    # Collapse a lone text part to a plain string: some backends only accept
    # the array form for genuinely multimodal turns.
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _turn_to_openai(role: str, content: Any) -> list[dict[str, Any]]:
    """ข้อความ Anthropic หนึ่งเทิร์น → ข้อความ chat หนึ่งตัวหรือมากกว่า ตามลำดับที่ต้องส่ง"""
    if isinstance(content, str):
        return [{"role": role, "content": content}] if content else []
    if not isinstance(content, list):
        return []

    tool_messages: list[dict[str, Any]] = []
    carried: list[dict[str, Any]] = []   # รูปจาก tool_result — ไปกับ user message ที่ตามมา
    parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(_text_part(block.get("text", "")))
        elif kind == "image":
            if image := _image_part(block):
                parts.append(image)
        elif kind == "document":
            parts.extend(document_parts(block) or [])
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:16]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
        elif kind == "tool_result":
            text, images = tool_result_parts(block) or ("", [])
            call_id = block.get("tool_use_id", "")
            tool_messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
            if images:
                carried.append(_text_part(f"[image returned by tool call {call_id}]"))
                carried.extend(images)
        # thinking / redacted_thinking: ความคิดของโมเดลอื่น ไม่มีที่ให้ใส่ใน chat completion
        # และ backend อ่านแล้วสับสนเปล่า ๆ — ทิ้งโดยตั้งใจ (ไม่ถูกนับในค่าประมาณด้วย)

    # Tool results must be emitted as their own role=tool messages, before
    # whatever else the same user turn contained.
    out = tool_messages
    parts = carried + parts
    if role == "assistant" and tool_calls:
        out.append({"role": role, "content": _collapse(parts) if parts else None,
                    "tool_calls": tool_calls})
    elif parts:
        out.append({"role": role, "content": _collapse(parts)})
    return out


def _wanted_format(body: dict[str, Any]) -> tuple[str, Any]:
    """(ตำแหน่งในคำขอ, ค่า) ของรูปแบบคำตอบที่ขอ — ตัวแปลกับตัวตรวจอ่านจากที่เดียวกัน"""
    config = body.get("output_config")
    wanted = config.get("format") if isinstance(config, dict) else None
    if wanted is not None:
        return "output_config.format", wanted
    return "output_format", body.get("output_format")      # ชื่อฟิลด์ช่วง beta


def _format_problem(body: dict[str, Any]) -> Problem | None:
    """รูปแบบคำตอบที่ขอไว้ แต่ `response_format` ข้างล่างแปลออกมาไม่ได้ — เดิมหายเงียบ ๆ

    ตรวจ 2026-10-09: `output_config.format = {"type": "json_schema"}` (ลืม schema) ได้ 200 และ
    backend ไม่ได้ `response_format` เลย · ดูตัวคู่ใน protocol/responses.py
    """
    path, wanted = _wanted_format(body)
    # ไม่ได้ขอ · ก้อนว่าง · หรือแปลได้ — ไม่มีอะไรหาย
    if wanted is None or wanted == {} or response_format(body) is not None:
        return None
    shape = '{"type": "json_schema", "schema": {...}}'
    if not isinstance(wanted, dict):
        return path, f"must be an object such as {shape}"
    if wanted.get("type") == "json_schema":
        return (
            f"{path}.schema",
            f"a json_schema format carries its schema here as an object: {shape}. "
            "Without it the model would not be held to any schema",
        )
    kind = wanted.get("type")
    return (
        f"{path}.type",
        (f"format type '{kind}' has" if kind is not None else "a format without a type has")
        + ' no chat-completions equivalent; the only structured output format is "json_schema"',
    )


def response_format(body: dict[str, Any]) -> dict[str, Any] | None:
    """`output_config.format` (structured outputs) → `response_format` ของ chat completions"""
    _, wanted = _wanted_format(body)
    if not isinstance(wanted, dict) or wanted.get("type") != "json_schema":
        return None
    schema = wanted.get("schema")
    if not isinstance(schema, dict):
        return None
    return {
        "type": "json_schema",
        "json_schema": {"name": wanted.get("name") or "response", "schema": schema,
                        "strict": True},
    }


def anthropic_to_openai_request(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    # ด่านสุดท้าย · ทางปกติถูกปฏิเสธไปตั้งแต่ด่าน capability แล้ว (ดู `untranslatable`)
    if problems := untranslatable(body):
        path, why = problems[0]
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK, f"{path}: {why}.", param=path
        )

    messages: list[dict] = []

    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "\n\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
        if text:
            messages.append({"role": "system", "content": text})

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        messages.extend(_turn_to_openai(message.get("role", "user"), message.get("content")))

    payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
    }
    for src, dst in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        # ไม่อยู่ในสเปกของ OpenAI แต่ vLLM · llama.cpp · Ollama · SGLang รับชื่อนี้ตรง ๆ
        ("top_k", "top_k"),
        ("stop_sequences", "stop"),
        ("stream", "stream"),
    ):
        if body.get(src) is not None:
            payload[dst] = body[src]

    if (wanted := response_format(body)) is not None:
        payload["response_format"] = wanted

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object"}),
                },
            }
            for tool in tools
            if isinstance(tool, dict)
        ]

    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        ctype = choice.get("type")
        if ctype == "auto":
            payload["tool_choice"] = "auto"
        elif ctype == "any":
            payload["tool_choice"] = "required"
        elif ctype == "tool" and choice.get("name"):
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": choice["name"]},
            }
        elif ctype == "none":
            payload["tool_choice"] = "none"
        if choice.get("disable_parallel_tool_use") is True:
            payload["parallel_tool_calls"] = False

    return payload


# ---------------------------------------------------------------------------
# OpenAI response -> Anthropic response
# ---------------------------------------------------------------------------
def openai_to_anthropic_response(
    payload: dict[str, Any], model_alias: str, *, include_thinking: bool = False
) -> dict[str, Any]:
    """`include_thinking` = ผู้เรียกขอ thinking มา (ดู wants_thinking)

    เดิม `reasoning_content` ถูกทิ้งเสมอ · โมเดล reasoning ที่ใช้ token หมดไปกับการคิดจึงคืน
    `content: [{"type": "text", "text": ""}]` กับ `stop_reason: "max_tokens"` — HTTP 200
    คิดเงินเต็ม และไม่มีอะไรบนจอบอกว่าโมเดลทำงานไปแล้วทั้งก้อน · ผู้เรียกที่ขอ thinking
    ตอนนี้เห็นว่ามันคิดอะไรอยู่และเห็นว่าทำไมไม่มีคำตอบ
    """
    choices = payload.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}

    content: list[dict] = []
    reasoning = _reasoning_text(message) if include_thinking else ""
    if reasoning:
        content.append(_thinking_block(reasoning))
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for block in text:
            if isinstance(block, dict) and block.get("type") == "text":
                content.append({"type": "text", "text": block.get("text", "")})

    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": function.get("arguments", "")}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id", f"toolu_{uuid.uuid4().hex[:16]}"),
                "name": function.get("name", ""),
                "input": arguments,
            }
        )

    if not content:
        content.append({"type": "text", "text": ""})

    usage = payload.get("usage") or {}
    return {
        "id": payload.get("id") or new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model_alias,
        "content": content,
        "stop_reason": _STOP_REASON_MAP.get(choice.get("finish_reason") or "stop", "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


class AnthropicStreamAdapter:
    """Convert an OpenAI SSE chunk stream into Anthropic Messages events.

    Emits the sequence Claude Code expects:
        message_start
        content_block_start / content_block_delta* / content_block_stop   (per block)
        message_delta (stop_reason + usage)
        message_stop

    Text, thinking and tool-call blocks are opened lazily, because an OpenAI
    stream does not announce block boundaries - it just starts sending deltas.
    """

    def __init__(
        self,
        model_alias: str,
        *,
        include_thinking: bool = False,
        input_tokens_estimate: int = 0,
    ) -> None:
        self.model_alias = model_alias
        self.message_id = new_message_id()
        self._include_thinking = include_thinking
        # ขนาด input ที่เกตเวย์ประมาณไว้ — ไปอยู่ใน message_start (ดู start_events)
        self._input_estimate = max(int(input_tokens_estimate), 0)
        self._started = False
        # block ที่เปิดค้างอยู่ตอนนี้: "thinking" · "text" · None — เปิดได้ทีละอัน
        self._open: str | None = None
        self._block_index = 0
        # openai tool_call index -> {"block": int, "id": str, "name": str}
        self._tool_blocks: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._closed = False

    def start_events(self) -> list[tuple[str, dict]]:
        if self._started:
            return []
        self._started = True
        return [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model_alias,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        # backend ที่พูด chat completions บอกขนาด input ใน chunk *สุดท้าย*
                        # แต่ message_start ต้องออกก่อน · ใส่ค่าประมาณของเกตเวย์ไว้ก่อน
                        # (เดิมเป็น 0 — client ที่วาดมิเตอร์ context จาก event นี้เห็น 0
                        # ตลอดทั้ง stream) ตัวเลขจริงตามไปใน message_delta
                        "usage": {"input_tokens": self._input_estimate, "output_tokens": 0},
                    },
                },
            )
        ]

    def handle_chunk(self, chunk: dict[str, Any]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        events.extend(self.start_events())

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage["input_tokens"] = int(
                usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            )
            self.usage["output_tokens"] = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )

        choices = chunk.get("choices") or []
        if not choices:
            return events
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}

        reasoning = _reasoning_text(delta) if self._include_thinking else ""
        if reasoning:
            events.extend(self._switch_to("thinking"))
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._block_index,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    },
                )
            )

        text = delta.get("content")
        if isinstance(text, str) and text:
            events.extend(self._switch_to("text"))
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._block_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )

        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            events.extend(self._handle_tool_call(call))

        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]

        return events

    def _close_open_block(self) -> list[tuple[str, dict]]:
        """ปิด block ข้อความ/ความคิดที่ค้างอยู่ แล้วเลื่อนไป index ถัดไป"""
        if self._open is None:
            return []
        self._open = None
        self._block_index += 1
        return [
            (
                "content_block_stop",
                {"type": "content_block_stop", "index": self._block_index - 1},
            )
        ]

    def _switch_to(self, kind: str) -> list[tuple[str, dict]]:
        """ให้ block ชนิด `kind` เป็นตัวที่เปิดอยู่ — ปิดตัวอื่นก่อนถ้ามี"""
        if self._open == kind:
            return []
        events = self._close_open_block()
        block = _thinking_block("") if kind == "thinking" else {"type": "text", "text": ""}
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._block_index,
                    "content_block": block,
                },
            )
        )
        self._open = kind
        return events

    def _handle_tool_call(self, call: dict[str, Any]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        index = int(call.get("index", 0))
        function = call.get("function") or {}

        if index not in self._tool_blocks:
            # A tool block always follows any text or thinking block; close it first.
            events.extend(self._close_open_block())

            block_index = self._block_index
            tool_id = call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"
            name = function.get("name", "")
            self._tool_blocks[index] = {"block": block_index, "id": tool_id, "name": name}
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": name,
                            "input": {},
                        },
                    },
                )
            )
            self._block_index += 1

        arguments = function.get("arguments")
        if arguments:
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._tool_blocks[index]["block"],
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    },
                )
            )
        return events

    def finish_events(
        self, *, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> list[tuple[str, dict]]:
        """ปิด block ที่ค้าง แล้วส่ง message_delta + message_stop

        `input_tokens` / `output_tokens` คือตัวเลขที่เกตเวย์บันทึกลงแถว usage (ของ backend
        ถ้ามันรายงาน ไม่งั้นค่าประมาณ) — ผู้เรียกต้องเห็นตัวเลขชุดเดียวกับที่ถูกคิด

        `message_delta.usage` มี `input_tokens` ด้วย: เดิมส่งแค่ `output_tokens` ผู้เรียกแบบ
        stream จึงไม่เคยถูกบอกขนาด input ที่แท้จริงเลย (message_start ออกไปก่อนจะรู้) ·
        API ของ Anthropic เองก็ใส่ input_tokens ใน message_delta และทั้ง SDK กับ Claude Code
        อ่านค่านั้นทับค่าจาก message_start
        """
        if self._closed:
            return []
        self._closed = True
        if input_tokens is not None:
            self.usage["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            self.usage["output_tokens"] = int(output_tokens)
        events: list[tuple[str, dict]] = []
        events.extend(self.start_events())

        events.extend(self._close_open_block())
        for meta in self._tool_blocks.values():
            events.append(
                ("content_block_stop", {"type": "content_block_stop", "index": meta["block"]})
            )

        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _STOP_REASON_MAP.get(
                            self._finish_reason or "stop", "end_turn"
                        ),
                        "stop_sequence": None,
                    },
                    "usage": {
                        "input_tokens": self.usage["input_tokens"],
                        "output_tokens": self.usage["output_tokens"],
                    },
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events
