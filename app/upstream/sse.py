"""Server-sent event helpers for streaming passthrough (FR-33)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

DONE = "[DONE]"


def format_sse(data: str, event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {data}\n\n".encode()


def format_json_sse(payload: dict[str, Any], event: str | None = None) -> bytes:
    return format_sse(json.dumps(payload, ensure_ascii=False), event)


async def iter_sse_payloads(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from an SSE line stream.

    Multi-line `data:` fields are joined with newlines per the SSE spec, so a
    backend that wraps a long JSON chunk across lines is handled correctly.
    """
    event: str | None = None
    data_lines: list[str] = []

    async for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        yield event, "\n".join(data_lines)


def parse_chunk(data: str) -> dict[str, Any] | None:
    if data.strip() == DONE:
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
