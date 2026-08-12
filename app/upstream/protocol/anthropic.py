"""Anthropic <-> OpenAI protocol translation (PRD §8, FR-25).

Claude Code speaks the Anthropic Messages API. Most local serving stacks (vLLM,
Ollama, SGLang) speak the OpenAI Chat Completions API. When a model's endpoint
declares `protocols.anthropic: true` the gateway forwards natively; otherwise it
translates here, in both directions, including the streaming event sequence.

Scope of the translation: text, images, system prompts, tool definitions,
tool_use / tool_result, stop reasons, usage. Anthropic-only features that have
no OpenAI equivalent (extended thinking blocks, citations, prompt caching hints)
are dropped on the way out and never fabricated on the way back.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

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


# ---------------------------------------------------------------------------
# Anthropic request -> OpenAI request
# ---------------------------------------------------------------------------
def _content_blocks_to_openai(content: Any) -> tuple[list[dict], list[dict]]:
    """Return (openai content parts, tool_calls) for one Anthropic message."""
    if isinstance(content, str):
        return ([{"type": "text", "text": content}] if content else []), []

    parts: list[dict] = []
    tool_calls: list[dict] = []
    if not isinstance(content, list):
        return parts, tool_calls

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/png")
                url = f"data:{media_type};base64,{source.get('data', '')}"
            else:
                url = source.get("url", "")
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_use":
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
        # thinking / redacted_thinking have no OpenAI equivalent: dropped.
    return parts, tool_calls


def _tool_results(content: Any) -> list[dict]:
    """Anthropic puts tool results in a user message; OpenAI uses role=tool."""
    results: list[dict] = []
    if not isinstance(content, list):
        return results
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        if isinstance(inner, list):
            text = "\n".join(
                b.get("text", "")
                for b in inner
                if isinstance(b, dict) and b.get("type") == "text"
            )
        elif isinstance(inner, str):
            text = inner
        else:
            text = json.dumps(inner, ensure_ascii=False) if inner is not None else ""
        results.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": text,
            }
        )
    return results


def anthropic_to_openai_request(body: dict[str, Any], upstream_model: str) -> dict[str, Any]:
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
        role = message.get("role", "user")
        content = message.get("content")

        # Tool results must be emitted as their own role=tool messages, before
        # whatever else the same user turn contained.
        results = _tool_results(content)
        if results:
            messages.extend(results)

        parts, tool_calls = _content_blocks_to_openai(content)
        if not parts and not tool_calls:
            continue

        entry: dict[str, Any] = {"role": role}
        if parts:
            # Collapse a lone text part to a plain string: some backends only
            # accept the array form for genuinely multimodal turns.
            if len(parts) == 1 and parts[0]["type"] == "text":
                entry["content"] = parts[0]["text"]
            else:
                entry["content"] = parts
        else:
            entry["content"] = None
        if tool_calls and role == "assistant":
            entry["tool_calls"] = tool_calls
        messages.append(entry)

    payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
    }
    for src, dst in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
        ("stream", "stream"),
    ):
        if body.get(src) is not None:
            payload[dst] = body[src]

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

    return payload


# ---------------------------------------------------------------------------
# OpenAI response -> Anthropic response
# ---------------------------------------------------------------------------
def openai_to_anthropic_response(
    payload: dict[str, Any], model_alias: str
) -> dict[str, Any]:
    choices = payload.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}

    content: list[dict] = []
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

    Text and tool-call blocks are opened lazily, because an OpenAI stream does
    not announce block boundaries - it just starts sending deltas.
    """

    def __init__(self, model_alias: str) -> None:
        self.model_alias = model_alias
        self.message_id = new_message_id()
        self._started = False
        self._text_open = False
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
                        "usage": {"input_tokens": 0, "output_tokens": 0},
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

        text = delta.get("content")
        if isinstance(text, str) and text:
            if not self._text_open:
                events.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": self._block_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
                self._text_open = True
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

    def _handle_tool_call(self, call: dict[str, Any]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        index = int(call.get("index", 0))
        function = call.get("function") or {}

        if index not in self._tool_blocks:
            # A tool block always follows any text block; close text first.
            if self._text_open:
                events.append(
                    (
                        "content_block_stop",
                        {"type": "content_block_stop", "index": self._block_index},
                    )
                )
                self._text_open = False
                self._block_index += 1

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

    def finish_events(self) -> list[tuple[str, dict]]:
        if self._closed:
            return []
        self._closed = True
        events: list[tuple[str, dict]] = []
        events.extend(self.start_events())

        if self._text_open:
            events.append(
                ("content_block_stop", {"type": "content_block_stop", "index": self._block_index})
            )
            self._text_open = False
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
                    "usage": {"output_tokens": self.usage["output_tokens"]},
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events
