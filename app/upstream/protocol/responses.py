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
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.upstream.protocol.reasoning import reasoning_text

__all__ = [
    "ResponsesStreamAdapter",
    "new_response_id",
    "openai_to_responses_response",
    "responses_to_openai_request",
]


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _item_id(prefix: str = "msg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Responses request -> OpenAI chat completions
# ---------------------------------------------------------------------------
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
        if ptype in {"input_text", "output_text", "text"}:
            parts.append({"type": "text", "text": part.get("text") or ""})
        elif ptype == "input_image":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url:
                image: dict[str, Any] = {"url": url}
                if part.get("detail"):
                    image["detail"] = part["detail"]
                parts.append({"type": "image_url", "image_url": image})

    if not parts:
        return None
    # Collapse a lone text part: some backends only accept the array form for
    # genuinely multimodal turns.
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def responses_to_openai_request(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    messages: list[dict] = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

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

    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "function_call":
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
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": output if isinstance(output, str) else json.dumps(output),
                }
            )
            continue

        if itype == "reasoning":
            # ไม่ส่งต่อ: เป็นร่องรอยความคิดของ *โมเดลอื่น* backend อ่านแล้วสับสนเปล่า ๆ
            continue

        role = item.get("role") or "user"
        content = _content_parts_to_openai(item.get("content"))
        if content is None:
            continue
        messages.append({"role": role, "content": content})

    flush_calls()

    payload: dict[str, Any] = {"model": upstream_model, "messages": messages}
    if body.get("max_output_tokens") is not None:
        payload["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if body.get(key) is not None:
            payload[key] = body[key]

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
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
            if isinstance(tool, dict) and tool.get("type") in (None, "function")
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

    def finish_events(self) -> list[tuple[str, dict]]:
        events = list(self.start_events())
        events.extend(self._close_open())

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

    def resume(self, response_id: str | None, next_sequence: int) -> None:
        """ต่อจาก stream ที่ backend พูด Responses เอง — ใช้ id และลำดับของมัน

        ทางที่ไม่ได้แปลไม่มี adapter เดินตาม แต่ถ้าสายขาดกลางทางก็ยังต้องปิดด้วย
        `response.failed` ที่มี id เดิมและ sequence_number ต่อจากตัวสุดท้ายที่ส่งไป
        """
        if response_id:
            self.response_id = response_id
        self._seq = max(self._seq, next_sequence)
        self._started = True

    def fail_events(self, code: str, message: str) -> list[tuple[str, dict]]:
        """ปิด stream ด้วย `response.failed` — event ปิดท้ายของ Responses เมื่อคำตอบไม่จบ

        เดิมสายไป backend ขาดกลางทางแล้ว stream หยุดหลัง `response.output_text.delta` เฉย ๆ:
        ไม่มี `response.completed` ไม่มี `response.failed` · Codex อ่านสายที่ปิดโดยไม่มี event
        ปิดท้ายว่า "stream closed before response.completed" ซึ่งไม่บอกอะไรเลยว่าเกิดอะไรขึ้น

        item ที่ส่งไปแล้วครึ่งทางไม่ถูกปิดทีละตัว: ผู้อ่านทิ้งทั้ง response เมื่อเห็น failed
        """
        events = list(self.start_events())
        final = self._skeleton("failed")
        final["output"] = [self._closed[index] for index in sorted(self._closed)]
        final["error"] = {"code": code, "message": message}
        final["usage"] = _usage_block(self.usage)
        events.append(self._next("response.failed", {"response": final}))
        return events
