#!/usr/bin/env python3
"""MODEL-001..010 compatibility suite (PRD §18, FR-36).

Runs the tests against a live gateway and posts each result back to
/admin/models/<alias>/compatibility, so the READY/DEGRADED badge in the console
reflects measurement rather than assumption (PRD §8).

    python scripts/model_test_suite.py --base-url http://localhost:8080 \
        --admin-key edu_sk_... --model coding

    # only the vision cases
    python scripts/model_test_suite.py ... --model gemma-vision --only MODEL-006,MODEL-007
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx

TEST_VERSION = "1.0"


# ---------------------------------------------------------------------------
def make_png(width: int = 128, height: int = 128) -> bytes:
    """A small PNG with a visible diagonal, so a vision model has something to say."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += b"\xff\x00\x00" if abs(x - y) < 6 else b"\xff\xff\xff"
        rows.append(bytes(row))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def png_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(make_png()).decode()


@dataclass
class Result:
    test_id: str
    feature: str
    status: str  # pass | fail | degraded | not_tested
    latency_ms: int = 0
    notes: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class Suite:
    def __init__(self, base_url: str, key: str, model: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    # -- helpers ---------------------------------------------------------
    def _chat(self, **payload: Any) -> httpx.Response:
        return self.client.post(
            "/v1/chat/completions", json={"model": self.model, **payload}
        )

    def _capabilities(self) -> dict[str, Any]:
        response = self.client.get("/v1/models")
        response.raise_for_status()
        for entry in response.json()["data"]:
            if entry["id"] == self.model:
                return entry
        raise SystemExit(f"model '{self.model}' is not visible to this key")

    # -- tests -----------------------------------------------------------
    def model_001_basic_chat(self) -> Result:
        started = time.perf_counter()
        response = self._chat(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=16,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return Result("MODEL-001", "chat", "fail", elapsed, _err(response))
        content = response.json()["choices"][0]["message"]["content"]
        return Result("MODEL-001", "chat", "pass", elapsed, f"replied {content!r}"[:200])

    def model_002_streaming(self) -> Result:
        started = time.perf_counter()
        ttft = None
        chunks = 0
        try:
            with self.client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Count from 1 to 5."}],
                    "max_tokens": 64,
                    "stream": True,
                },
            ) as response:
                if response.status_code != 200:
                    return Result("MODEL-002", "streaming", "fail", 0, _err(response))
                for line in response.iter_lines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        if ttft is None:
                            ttft = int((time.perf_counter() - started) * 1000)
                        chunks += 1
        except Exception as exc:
            return Result("MODEL-002", "streaming", "fail", 0, str(exc)[:200])
        if chunks < 2:
            return Result(
                "MODEL-002", "streaming", "degraded", ttft or 0, f"only {chunks} chunk(s)"
            )
        return Result("MODEL-002", "streaming", "pass", ttft or 0, f"{chunks} chunks")

    def model_003_long_context(self) -> Result:
        caps = self._capabilities()
        # ~25% of the window, so the test is meaningful but not punishing.
        target_tokens = max(int(caps["context_window"] * 0.25), 512)
        filler = "The quick brown fox jumps over the lazy dog. " * (target_tokens // 10)
        started = time.perf_counter()
        response = self._chat(
            messages=[
                {
                    "role": "user",
                    "content": f"{filler}\n\nHow many times did the word 'fox' appear? "
                    "Answer with a number only.",
                }
            ],
            max_tokens=32,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return Result("MODEL-003", "long_context", "fail", elapsed, _err(response))
        used = response.json().get("usage", {}).get("prompt_tokens", 0)
        return Result(
            "MODEL-003", "long_context", "pass", elapsed, f"{used} prompt tokens accepted"
        )

    def _skip_without_tools(self, test_id: str, feature: str) -> Result | None:
        """A model that declares tools=false is not broken - the test is N/A.

        Without this the gateway's own (correct) MODEL_CAPABILITY_NOT_SUPPORTED
        rejection would be recorded as a failure and drag the model to DEGRADED.
        """
        if not self._capabilities()["capabilities"].get("tools"):
            return Result(test_id, feature, "not_tested", 0, "model declares tools=false")
        return None

    def model_004_tool_calling(self) -> Result:
        skip = self._skip_without_tools("MODEL-004", "tools")
        if skip:
            return skip
        started = time.perf_counter()
        response = self._chat(
            messages=[{"role": "user", "content": "What is the weather in Bangkok?"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
            max_tokens=128,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return Result("MODEL-004", "tools", "fail", elapsed, _err(response))
        message = response.json()["choices"][0]["message"]
        if not message.get("tool_calls"):
            return Result(
                "MODEL-004", "tools", "degraded", elapsed, "no tool_calls in response"
            )
        name = message["tool_calls"][0]["function"]["name"]
        return Result("MODEL-004", "tools", "pass", elapsed, f"called {name}")

    def model_005_multi_tool(self) -> Result:
        skip = self._skip_without_tools("MODEL-005", "multi_tool")
        if skip:
            return skip
        started = time.perf_counter()
        response = self._chat(
            messages=[
                {"role": "user", "content": "Get the weather in Bangkok AND in Chiang Mai."}
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
            max_tokens=256,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return Result("MODEL-005", "multi_tool", "fail", elapsed, _err(response))
        calls = response.json()["choices"][0]["message"].get("tool_calls") or []
        status = "pass" if len(calls) >= 2 else "degraded"
        return Result("MODEL-005", "multi_tool", status, elapsed, f"{len(calls)} call(s)")

    def model_006_vision(self) -> Result:
        caps = self._capabilities()
        if not caps["capabilities"].get("vision"):
            return Result("MODEL-006", "vision", "not_tested", 0, "model declares vision=false")
        started = time.perf_counter()
        response = self._chat(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": png_data_url()}},
                        {"type": "text", "text": "Describe this image in one sentence."},
                    ],
                }
            ],
            max_tokens=128,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return Result("MODEL-006", "vision", "fail", elapsed, _err(response))
        body = response.json()
        visual = body.get("usage", {}).get("edullm", {}).get("visual_input_tokens", 0)
        return Result(
            "MODEL-006", "vision", "pass", elapsed, f"visual_input_tokens={visual}"
        )

    def model_007_vision_plus_text(self) -> Result:
        caps = self._capabilities()
        if not caps["capabilities"].get("vision"):
            return Result(
                "MODEL-007", "vision_text", "not_tested", 0, "model declares vision=false"
            )
        started = time.perf_counter()
        response = self._chat(
            messages=[
                {"role": "user", "content": "I will show you a diagram."},
                {"role": "assistant", "content": "Please go ahead."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What colour is the diagonal line?"},
                        {"type": "image_url", "image_url": {"url": png_data_url()}},
                    ],
                },
            ],
            max_tokens=128,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return Result("MODEL-007", "vision_text", "fail", elapsed, _err(response))
        answer = response.json()["choices"][0]["message"]["content"] or ""
        status = "pass" if "red" in answer.lower() else "degraded"
        return Result(
            "MODEL-007", "vision_text", status, elapsed, f"answer: {answer[:120]!r}"
        )

    def model_008_agent_loop(self) -> Result:
        """Two turns with a tool result fed back - the core agentic pattern."""
        skip = self._skip_without_tools("MODEL-008", "agent_loop")
        if skip:
            return skip
        started = time.perf_counter()
        first = self._chat(
            messages=[{"role": "user", "content": "Read the file main.py."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
            max_tokens=128,
        )
        if first.status_code != 200:
            return Result("MODEL-008", "agent_loop", "fail", 0, _err(first))
        message = first.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return Result("MODEL-008", "agent_loop", "degraded", 0, "no initial tool call")

        second = self._chat(
            messages=[
                {"role": "user", "content": "Read the file main.py."},
                message,
                {
                    "role": "tool",
                    "tool_call_id": calls[0]["id"],
                    "content": "print('hello world')",
                },
            ],
            max_tokens=128,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if second.status_code != 200:
            return Result("MODEL-008", "agent_loop", "fail", elapsed, _err(second))
        return Result("MODEL-008", "agent_loop", "pass", elapsed, "tool result accepted")

    def model_009_claude_code(self) -> Result:
        """Exercise the Anthropic surface exactly as Claude Code would."""
        started = time.perf_counter()
        response = self.client.post(
            "/v1/messages",
            json={
                "model": self.model,
                "max_tokens": 128,
                "system": "You are a coding assistant.",
                "messages": [{"role": "user", "content": "Say OK."}],
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read a file",
                        "input_schema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    }
                ],
            },
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code == 400 and "PROTOCOL_NOT_SUPPORTED" in response.text:
            return Result(
                "MODEL-009", "claude_code", "not_tested", elapsed,
                "protocols.anthropic is false for this alias",
            )
        if response.status_code != 200:
            return Result("MODEL-009", "claude_code", "fail", elapsed, _err(response))
        body = response.json()
        if body.get("type") != "message" or not body.get("content"):
            return Result(
                "MODEL-009", "claude_code", "degraded", elapsed, "unexpected response shape"
            )
        return Result(
            "MODEL-009", "claude_code", "pass", elapsed,
            f"stop_reason={body.get('stop_reason')}",
        )

    def model_010_concurrent_load(self, concurrency: int = 5) -> Result:
        import concurrent.futures

        started = time.perf_counter()

        def one(i: int) -> int:
            return self._chat(
                messages=[{"role": "user", "content": f"Say {i}."}], max_tokens=16
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            codes = list(pool.map(one, range(concurrency)))
        elapsed = int((time.perf_counter() - started) * 1000)

        ok = sum(1 for c in codes if c == 200)
        throttled = sum(1 for c in codes if c == 429)
        if ok == concurrency:
            status = "pass"
        elif ok + throttled == concurrency:
            status = "degraded"  # limits applied, nothing broke
        else:
            status = "fail"
        return Result(
            "MODEL-010", "concurrent", status, elapsed,
            f"{ok} ok / {throttled} throttled / {concurrency} total",
        )

    def run(self, only: set[str] | None = None) -> list[Result]:
        tests = [
            ("MODEL-001", self.model_001_basic_chat),
            ("MODEL-002", self.model_002_streaming),
            ("MODEL-003", self.model_003_long_context),
            ("MODEL-004", self.model_004_tool_calling),
            ("MODEL-005", self.model_005_multi_tool),
            ("MODEL-006", self.model_006_vision),
            ("MODEL-007", self.model_007_vision_plus_text),
            ("MODEL-008", self.model_008_agent_loop),
            ("MODEL-009", self.model_009_claude_code),
            ("MODEL-010", self.model_010_concurrent_load),
        ]
        results = []
        for test_id, fn in tests:
            if only and test_id not in only:
                continue
            print(f"  {test_id} ... ", end="", flush=True)
            try:
                result = fn()
            except Exception as exc:
                result = Result(test_id, test_id.lower(), "fail", 0, f"exception: {exc}")
            symbol = {"pass": "PASS", "fail": "FAIL", "degraded": "DEGRADED"}.get(
                result.status, "SKIP"
            )
            print(f"{symbol:9s} {result.latency_ms:>6d} ms  {result.notes}")
            results.append(result)
        return results

    def publish(self, results: list[Result]) -> None:
        # `not_tested` is published too, not skipped: it is what clears a stale
        # `fail` recorded before the model's capabilities were declared
        # correctly. Skipping it leaves the model DEGRADED forever.
        for result in results:
            response = self.client.post(
                f"/admin/models/{self.model}/compatibility",
                json={
                    "feature": result.feature,
                    "status": result.status,
                    "test_version": TEST_VERSION,
                    "latency_ms": result.latency_ms,
                    "notes": f"{result.test_id}: {result.notes}"[:500],
                },
            )
            if response.status_code >= 400:
                print(f"    ! could not publish {result.test_id}: {response.text[:120]}")


def _err(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", body)
        code, message = error.get("code", ""), error.get("message", "")
        return f"HTTP {response.status_code} {code}: {message}"[:300]
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:200]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="EduLLM Gateway model test suite")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--model", required=True, help="model alias to test")
    parser.add_argument("--only", default="", help="comma-separated test ids")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--no-publish", action="store_true", help="do not post results to the gateway"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON to stdout")
    args = parser.parse_args()

    only = {t.strip().upper() for t in args.only.split(",") if t.strip()} or None
    suite = Suite(args.base_url, args.admin_key, args.model, args.timeout)

    print(f"\nModel test suite v{TEST_VERSION} - {args.model} @ {args.base_url}\n")
    results = suite.run(only)

    if not args.no_publish:
        suite.publish(results)

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    degraded = sum(1 for r in results if r.status == "degraded")
    skipped = sum(1 for r in results if r.status == "not_tested")
    print(
        f"\n{passed} passed, {failed} failed, {degraded} degraded, {skipped} skipped"
    )

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=False))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
