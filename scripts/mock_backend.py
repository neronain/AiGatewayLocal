#!/usr/bin/env python3
"""A stand-in vLLM server, for validating a deployment without GPU hardware.

Implements enough of the OpenAI surface for the gateway's full request path:
/health, /v1/models, /v1/chat/completions (streaming and not, tools, vision).

    python scripts/mock_backend.py --port 8000 --model ucbye/Qwen3-Coder-Next-NVFP4-GB10

Never use this in production - it generates canned text, not inference.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="mock-vllm")
SERVED_MODEL = "mock-model"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": SERVED_MODEL, "object": "model", "owned_by": "mock"}],
    }


def _describe(body: dict[str, Any]) -> tuple[str, int]:
    """Produce a reply that proves what actually arrived at the backend."""
    images = 0
    text_parts: list[str] = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    images += 1

    if images:
        return (
            f"[mock] Received {images} image(s) and "
            f"{len(' '.join(text_parts))} characters of text. "
            "The diagonal line in the image is red.",
            images,
        )
    last = text_parts[-1] if text_parts else ""
    if "Reply with exactly: OK" in last:
        return "OK", 0
    if "count from 1 to 5" in last.lower():
        return "1 2 3 4 5", 0
    return f"[mock] echo: {last[:200]}", 0


def _tool_call(body: dict[str, Any]) -> list[dict] | None:
    tools = body.get("tools") or []
    if not tools:
        return None
    last = ""
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            content = message.get("content")
            last = content if isinstance(content, str) else json.dumps(content)
            break
    if message_has_tool_result(body):
        return None

    name = tools[0].get("function", {}).get("name", "unknown")
    cities = [c for c in ("Bangkok", "Chiang Mai") if c.lower() in last.lower()]
    if len(cities) >= 2:
        return [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps({"city": city}),
                },
            }
            for i, city in enumerate(cities)
        ]
    argument = {"city": cities[0]} if cities else {"path": "main.py"}
    return [
        {
            "id": "call_0",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(argument)},
        }
    ]


def message_has_tool_result(body: dict[str, Any]) -> bool:
    return any(m.get("role") == "tool" for m in body.get("messages", []))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text, images = _describe(body)
    tool_calls = _tool_call(body)
    prompt_tokens = 20 + images * 900
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if body.get("stream"):

        async def stream():
            include_usage = (body.get("stream_options") or {}).get("include_usage")
            if tool_calls:
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model", SERVED_MODEL),
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            else:
                for word in text.split(" "):
                    chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": body.get("model", SERVED_MODEL),
                        "choices": [
                            {"index": 0, "delta": {"content": word + " "}, "finish_reason": None}
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n".encode()

            final = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", SERVED_MODEL),
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if tool_calls else "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(final)}\n\n".encode()

            if include_usage:
                usage_chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model", SERVED_MODEL),
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": len(text.split()),
                        "total_tokens": prompt_tokens + len(text.split()),
                    },
                }
                yield f"data: {json.dumps(usage_chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    message: dict[str, Any] = {"role": "assistant", "content": None if tool_calls else text}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", SERVED_MODEL),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(text.split()),
            "total_tokens": prompt_tokens + len(text.split()),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible backend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default="mock-model")
    args = parser.parse_args()
    SERVED_MODEL = args.model
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
