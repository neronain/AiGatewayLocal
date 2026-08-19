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
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

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
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(
            {
                "id": _item_id(),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )

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
        "incomplete_details": ({"reason": "max_output_tokens"} if status == "incomplete" else None),
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
        response.output_item.added / response.content_part.added
        response.output_text.delta*
        response.content_part.done / response.output_item.done
        response.completed

    Every event carries a `sequence_number`; the client uses it to detect a gap,
    so it has to increase by one across *all* event types, not per type.
    """

    def __init__(self, model_alias: str) -> None:
        self.model_alias = model_alias
        self.response_id = new_response_id()
        self._seq = 0
        self._started = False
        self._text_item: str | None = None
        self._text = ""
        self._output_index = 0
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

        text = delta.get("content")
        if isinstance(text, str) and text:
            if self._text_item is None:
                self._text_item = _item_id()
                events.append(
                    self._next(
                        "response.output_item.added",
                        {
                            "output_index": self._output_index,
                            "item": {
                                "id": self._text_item,
                                "type": "message",
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        },
                    )
                )
                events.append(
                    self._next(
                        "response.content_part.added",
                        {
                            "item_id": self._text_item,
                            "output_index": self._output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    )
                )
            self._text += text
            events.append(
                self._next(
                    "response.output_text.delta",
                    {
                        "item_id": self._text_item,
                        "output_index": self._output_index,
                        "content_index": 0,
                        "delta": text,
                    },
                )
            )

        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict):
                events.extend(self._handle_tool_call(call))

        return events

    def _handle_tool_call(self, call: dict[str, Any]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        index = int(call.get("index") or 0)
        fn = call.get("function") or {}

        state = self._tools.get(index)
        if state is None:
            # A text item, if any, is closed before a tool item opens: the two
            # must not be open at the same output_index.
            events.extend(self._close_text())
            self._output_index += 1 if self._text_item is not None else 0
            state = {
                "item_id": _item_id("fc"),
                "call_id": call.get("id") or _item_id("call"),
                "name": fn.get("name") or "",
                "args": "",
                "output_index": self._output_index,
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
            self._output_index += 1

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

    def _close_text(self) -> list[tuple[str, dict]]:
        if self._text_item is None:
            return []
        item_id, self._text_item_closed = self._text_item, True
        events = [
            self._next(
                "response.output_text.done",
                {
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": self._text,
                },
            ),
            self._next(
                "response.content_part.done",
                {
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": self._text, "annotations": []},
                },
            ),
            self._next(
                "response.output_item.done",
                {
                    "output_index": 0,
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": self._text, "annotations": []}
                        ],
                    },
                },
            ),
        ]
        self._text_item = None
        return events

    def finish_events(self) -> list[tuple[str, dict]]:
        events = list(self.start_events())
        events.extend(self._close_text())

        output: list[dict] = []
        if self._text:
            output.append(
                {
                    "id": _item_id(),
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": self._text, "annotations": []}],
                }
            )
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
            output.append(item)

        status = _STATUS_FOR_FINISH.get(self._finish or "stop", "completed")
        final = self._skeleton(status)
        final["output"] = output
        final["output_text"] = self._text
        final["usage"] = _usage_block(self.usage)
        if status == "incomplete":
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        events.append(
            self._next(
                "response.completed" if status == "completed" else "response.incomplete",
                {"response": final},
            )
        )
        return events
