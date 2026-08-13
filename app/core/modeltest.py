"""MODEL-001..010 compatibility suite and backend capability probing.

Single source of truth for the tests: the CLI (`scripts/model_test_suite.py`)
and the admin API (`POST /admin/models/{alias}/test`) both drive this module, so
the console badge and the terminal run can never disagree.

Everything here talks to the gateway over HTTP with the caller's own API key.
Running the suite therefore exercises the exact path a member would take -
auth, policy, capability gates, quota and routing included.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.core.passwords import SESSION_COOKIE

TEST_VERSION = "1.1"

# Tests whose feature name maps onto a capability flag. When the model declares
# the flag false the test is not applicable - recording it as a failure would
# report a correctly-configured model as DEGRADED.
_REQUIRES_TOOLS = {"MODEL-004", "MODEL-005", "MODEL-008"}
_REQUIRES_VISION = {"MODEL-006", "MODEL-007"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_png(width: int = 128, height: int = 128) -> bytes:
    """A PNG with a red diagonal on white, so a vision model has something to say."""

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
class TestResult:
    test_id: str
    feature: str
    status: str  # pass | fail | degraded | not_tested
    latency_ms: int = 0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SuiteSummary:
    passed: int = 0
    failed: int = 0
    degraded: int = 0
    skipped: int = 0
    results: list[TestResult] = field(default_factory=list)

    @classmethod
    def of(cls, results: list[TestResult]) -> SuiteSummary:
        return cls(
            passed=sum(1 for r in results if r.status == "pass"),
            failed=sum(1 for r in results if r.status == "fail"),
            degraded=sum(1 for r in results if r.status == "degraded"),
            skipped=sum(1 for r in results if r.status == "not_tested"),
            results=results,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "degraded": self.degraded,
            "skipped": self.skipped,
            "results": [r.to_dict() for r in self.results],
        }


ProgressCallback = Callable[[TestResult], Awaitable[None]]


def _short_error(response: httpx.Response) -> str:
    """The backend's own message, which usually names the missing flag."""
    try:
        body = response.json()
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        return json.dumps(body)[:200]
    except Exception:
        return response.text[:200]


def _describe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", body)
        code, message = error.get("code", ""), error.get("message", "")
        return f"HTTP {response.status_code} {code}: {message}"[:300]
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:200]}"


class ModelTestSuite:
    """Runs MODEL-001..010 against a live gateway."""

    ALL_TESTS = (
        "MODEL-001",
        "MODEL-002",
        "MODEL-003",
        "MODEL-004",
        "MODEL-005",
        "MODEL-006",
        "MODEL-007",
        "MODEL-008",
        "MODEL-009",
        "MODEL-010",
    )

    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: float = 180.0,
        session_cookie: str = "",
    ) -> None:
        """`api_key` or `session_cookie` — whichever the caller authenticated with.

        The suite drives the public API, and that accepts either: a program sends
        a key, the console sends a cookie. Taking only the key meant every run
        started from the console arrived with no credential at all and each test
        came back `MISSING_API_KEY` — the console being the only place the button
        exists.

        Whichever it is, it is the caller's own authority, held for the run and
        never written down. Minting a key here instead would leave a row behind
        for something that lasts seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._caps: dict[str, Any] | None = None
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        cookies = {SESSION_COOKIE: session_cookie} if session_cookie else None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- helpers ---------------------------------------------------------
    async def _chat(self, **payload: Any) -> httpx.Response:
        return await self._client.post(
            "/v1/chat/completions", json={"model": self.model, **payload}
        )

    async def capabilities(self) -> dict[str, Any]:
        if self._caps is None:
            response = await self._client.get("/v1/models")
            response.raise_for_status()
            for entry in response.json()["data"]:
                if entry["id"] == self.model:
                    self._caps = entry
                    break
            else:
                raise LookupError(f"model '{self.model}' is not visible to this key")
        return self._caps

    async def _declares(self, capability: str) -> bool:
        caps = await self.capabilities()
        return bool(caps["capabilities"].get(capability))

    # -- tests -----------------------------------------------------------
    async def model_001(self) -> TestResult:
        started = time.perf_counter()
        response = await self._chat(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=16
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return TestResult("MODEL-001", "chat", "fail", elapsed, _describe_error(response))
        content = response.json()["choices"][0]["message"].get("content")
        return TestResult("MODEL-001", "chat", "pass", elapsed, f"replied {content!r}"[:200])

    async def model_002(self) -> TestResult:
        started = time.perf_counter()
        ttft: int | None = None
        chunks = 0
        try:
            async with self._client.stream(
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
                    await response.aread()
                    return TestResult(
                        "MODEL-002", "streaming", "fail", 0, _describe_error(response)
                    )
                async for line in response.aiter_lines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        if ttft is None:
                            ttft = int((time.perf_counter() - started) * 1000)
                        chunks += 1
        except Exception as exc:
            return TestResult("MODEL-002", "streaming", "fail", 0, str(exc)[:200])
        if chunks < 2:
            return TestResult(
                "MODEL-002", "streaming", "degraded", ttft or 0, f"only {chunks} chunk(s)"
            )
        return TestResult("MODEL-002", "streaming", "pass", ttft or 0, f"{chunks} chunks")

    async def model_003(self) -> TestResult:
        caps = await self.capabilities()
        target = max(int(caps["context_window"] * 0.25), 512)
        filler = "The quick brown fox jumps over the lazy dog. " * (target // 10)
        started = time.perf_counter()
        response = await self._chat(
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
            return TestResult(
                "MODEL-003", "long_context", "fail", elapsed, _describe_error(response)
            )
        used = response.json().get("usage", {}).get("prompt_tokens", 0)
        return TestResult(
            "MODEL-003", "long_context", "pass", elapsed, f"{used} prompt tokens accepted"
        )

    async def model_004(self) -> TestResult:
        started = time.perf_counter()
        response = await self._chat(
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
            return TestResult("MODEL-004", "tools", "fail", elapsed, _describe_error(response))
        message = response.json()["choices"][0]["message"]
        if not message.get("tool_calls"):
            return TestResult(
                "MODEL-004", "tools", "degraded", elapsed, "no tool_calls in response"
            )
        name = message["tool_calls"][0]["function"]["name"]
        return TestResult("MODEL-004", "tools", "pass", elapsed, f"called {name}")

    async def model_005(self) -> TestResult:
        started = time.perf_counter()
        response = await self._chat(
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
            return TestResult(
                "MODEL-005", "multi_tool", "fail", elapsed, _describe_error(response)
            )
        calls = response.json()["choices"][0]["message"].get("tool_calls") or []
        status = "pass" if len(calls) >= 2 else "degraded"
        return TestResult("MODEL-005", "multi_tool", status, elapsed, f"{len(calls)} call(s)")

    async def model_006(self) -> TestResult:
        started = time.perf_counter()
        response = await self._chat(
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
            return TestResult("MODEL-006", "vision", "fail", elapsed, _describe_error(response))
        visual = (
            response.json().get("usage", {}).get("litegate", {}).get("visual_input_tokens", 0)
        )
        return TestResult("MODEL-006", "vision", "pass", elapsed, f"visual_input_tokens={visual}")

    async def model_007(self) -> TestResult:
        started = time.perf_counter()
        response = await self._chat(
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
            return TestResult(
                "MODEL-007", "vision_text", "fail", elapsed, _describe_error(response)
            )
        answer = response.json()["choices"][0]["message"].get("content") or ""
        status = "pass" if "red" in answer.lower() else "degraded"
        return TestResult("MODEL-007", "vision_text", status, elapsed, f"answer: {answer[:120]!r}")

    async def model_008(self) -> TestResult:
        started = time.perf_counter()
        first = await self._chat(
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
            return TestResult("MODEL-008", "agent_loop", "fail", 0, _describe_error(first))
        message = first.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return TestResult("MODEL-008", "agent_loop", "degraded", 0, "no initial tool call")

        second = await self._chat(
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
            return TestResult(
                "MODEL-008", "agent_loop", "fail", elapsed, _describe_error(second)
            )
        return TestResult("MODEL-008", "agent_loop", "pass", elapsed, "tool result accepted")

    async def model_009(self) -> TestResult:
        # Claude Code needs tool calling. When the model has none we still
        # exercise the Anthropic surface - without tools, so the gateway's own
        # capability gate does not reject the request - and report the result as
        # degraded rather than pass: the protocol works, the client will not.
        has_tools = await self._declares("tools")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 128,
            "system": "You are a coding assistant.",
            "messages": [{"role": "user", "content": "Say OK."}],
        }
        if has_tools:
            payload["tools"] = [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ]

        started = time.perf_counter()
        response = await self._client.post("/v1/messages", json=payload)
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code == 400 and "PROTOCOL_NOT_SUPPORTED" in response.text:
            return TestResult(
                "MODEL-009",
                "claude_code",
                "not_tested",
                elapsed,
                "protocols.anthropic is false for this alias",
            )
        if response.status_code != 200:
            return TestResult(
                "MODEL-009", "claude_code", "fail", elapsed, _describe_error(response)
            )
        body = response.json()
        if body.get("type") != "message" or not body.get("content"):
            return TestResult(
                "MODEL-009", "claude_code", "degraded", elapsed, "unexpected response shape"
            )
        if not has_tools:
            return TestResult(
                "MODEL-009",
                "claude_code",
                "degraded",
                elapsed,
                "Anthropic surface works, but the model has no tool calling - "
                "Claude Code needs it",
            )
        return TestResult(
            "MODEL-009", "claude_code", "pass", elapsed, f"stop_reason={body.get('stop_reason')}"
        )

    async def model_010(self, concurrency: int = 5) -> TestResult:
        started = time.perf_counter()

        async def one(index: int) -> int:
            response = await self._chat(
                messages=[{"role": "user", "content": f"Say {index}."}], max_tokens=16
            )
            return response.status_code

        codes = await asyncio.gather(
            *(one(i) for i in range(concurrency)), return_exceptions=True
        )
        elapsed = int((time.perf_counter() - started) * 1000)

        ok = sum(1 for c in codes if c == 200)
        throttled = sum(1 for c in codes if c == 429)
        if ok == concurrency:
            status = "pass"
        elif ok + throttled == concurrency:
            status = "degraded"  # limits applied, nothing actually broke
        else:
            status = "fail"
        return TestResult(
            "MODEL-010",
            "concurrent",
            status,
            elapsed,
            f"{ok} ok / {throttled} throttled / {concurrency} total",
        )

    # -- driver ----------------------------------------------------------
    async def _skip_reason(self, test_id: str) -> str | None:
        if test_id in _REQUIRES_TOOLS and not await self._declares("tools"):
            return "model declares tools=false"
        if test_id in _REQUIRES_VISION and not await self._declares("vision"):
            return "model declares vision=false"
        return None

    _FEATURES = {
        "MODEL-001": "chat",
        "MODEL-002": "streaming",
        "MODEL-003": "long_context",
        "MODEL-004": "tools",
        "MODEL-005": "multi_tool",
        "MODEL-006": "vision",
        "MODEL-007": "vision_text",
        "MODEL-008": "agent_loop",
        "MODEL-009": "claude_code",
        "MODEL-010": "concurrent",
    }

    async def run(
        self,
        only: set[str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[TestResult]:
        results: list[TestResult] = []
        for test_id in self.ALL_TESTS:
            if only and test_id not in only:
                continue
            feature = self._FEATURES[test_id]
            try:
                reason = await self._skip_reason(test_id)
                if reason:
                    result = TestResult(test_id, feature, "not_tested", 0, reason)
                else:
                    method = getattr(self, f"model_{test_id.split('-')[1]}")
                    result = await method()
            except Exception as exc:
                note = f"{type(exc).__name__}: {exc}"[:250]
                result = TestResult(test_id, feature, "fail", 0, note)
            results.append(result)
            if progress:
                await progress(result)
        return results

    async def publish(self, results: list[TestResult]) -> None:
        """Record results on the gateway.

        `not_tested` is published too: it is what clears a stale `fail` recorded
        before the model's capabilities were declared correctly.
        """
        for result in results:
            try:
                await self._client.post(
                    f"/admin/models/{self.model}/compatibility",
                    json={
                        "feature": result.feature,
                        "status": result.status,
                        "test_version": TEST_VERSION,
                        "latency_ms": result.latency_ms,
                        "notes": f"{result.test_id}: {result.notes}"[:500],
                    },
                )
            except Exception:  # publishing is bookkeeping, not the test itself
                continue


# ---------------------------------------------------------------------------
# Backend capability probe (PRD FR-39)
# ---------------------------------------------------------------------------
@dataclass
class Advice:
    """A finding with its remediation attached.

    Reporting that a backend cannot call tools is half an answer: whoever reads
    it still has to work out which flag is missing and what to run. Every entry
    carries the fix, and `command` is meant to be pasted.
    """

    issue: str
    severity: str  # blocker | warning | info
    detail: str
    fix: str
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# vLLM ships one tool parser per model family and picking the wrong one fails
# quietly - the server starts and simply never emits tool_calls. So suggest only
# where the served name is unambiguous, and say when it is a guess.
_PARSER_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("qwen3-coder", "qwen3_coder", "qwen3coder"), "qwen3_coder"),
    (("llama-4", "llama4"), "llama4_pythonic"),
    (("llama-3", "llama3"), "llama3_json"),
    (("mistral", "mixtral"), "mistral"),
    (("deepseek-v3", "deepseek_v3"), "deepseek_v3"),
    (("glm-4", "glm4"), "glm45"),
    (("kimi", "k2"), "kimi_k2"),
    (("qwen",), "hermes"),
)


def suggest_tool_parser(served_name: str) -> tuple[str, bool]:
    """Return (parser, confident). Never guesses silently."""
    name = (served_name or "").lower()
    for needles, parser in _PARSER_HINTS:
        if any(n in name for n in needles):
            return parser, len(needles) > 1 or needles[0] != "qwen"
    return "hermes", False


@dataclass
class ProbeResult:
    reachable: bool = False
    served_models: list[str] = field(default_factory=list)
    upstream_model: str = ""
    context_tokens: int | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    protocols: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    advice: list[Advice] = field(default_factory=list)
    server_kind: str = ""  # vllm | llamacpp | unknown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _detect_server_kind(notes: list[str], served: list[str]) -> str:
    blob = " ".join(notes).lower()
    if "vllm" in blob or "tool choice requires" in blob:
        return "vllm"
    if "mmproj" in blob or "llama" in blob:
        return "llamacpp"
    return "unknown"


def resolve_commands(advice: list[Advice], managed_by: object | None) -> list[Advice]:
    """Turn `./<controller>.sh ...` into the command for this actual backend.

    Advice with a placeholder in it is advice someone still has to translate.
    When the registry says which deploy tool produced a backend and where its
    controller lives, the finding can name the command outright.

    Nothing here contacts the deploy tool - this is string substitution against
    what the registry already declares.
    """
    node = getattr(managed_by, "node", "") if managed_by else ""
    controller = getattr(managed_by, "controller", "") if managed_by else ""
    if not controller:
        return advice

    invocation = f"{controller}" if not node else f"ssh {node} '{controller}"
    closing = "" if not node else "'"
    resolved: list[Advice] = []
    for item in advice:
        if "<controller>" in item.command:
            tail = item.command.split("./<controller>.sh", 1)[1].strip()
            item = Advice(
                issue=item.issue,
                severity=item.severity,
                detail=item.detail,
                fix=item.fix,
                command=f"{invocation} {tail}{closing}",
            )
        resolved.append(item)
    return resolved


def build_advice(result: ProbeResult) -> list[Advice]:
    """Turn what was measured into what to do about it.

    Grounded in the two failures this fleet actually hit: a vLLM started without
    a tool parser, and a vision-capable GGUF served without its projector.
    """
    advice: list[Advice] = []
    notes = " ".join(result.notes).lower()
    model = result.upstream_model or (result.served_models[0] if result.served_models else "")

    if not result.reachable:
        advice.append(
            Advice(
                issue="unreachable",
                severity="blocker",
                detail="The backend did not answer /v1/models.",
                fix="Check the server is running and the base URL is reachable "
                "from the gateway host.",
            )
        )
        return advice

    if not result.capabilities.get("tools"):
        parser, confident = suggest_tool_parser(model)
        if "tool choice requires" in notes or "enable-auto-tool-choice" in notes:
            advice.append(
                Advice(
                    issue="tools_flag_missing",
                    severity="warning",
                    detail="vLLM rejected the tool request: it was started without "
                    "--enable-auto-tool-choice and a --tool-call-parser.",
                    fix=(
                        f"Restart with the parser for this model "
                        f"({parser}{'' if confident else ', unverified guess'}). "
                        "An LMDS controller takes it as an option; a hand-written "
                        "unit needs the two flags added."
                    ),
                    command=f"./<controller>.sh restart --tool-parser {parser}",
                )
            )
        else:
            advice.append(
                Advice(
                    issue="tools_unavailable",
                    severity="info",
                    detail="The backend accepted a tool request but returned no "
                    "tool_calls.",
                    fix="Either the model has no tool template, or the parser does "
                    "not match it. Declare tools=false unless you can make it "
                    "emit tool_calls.",
                )
            )

    if "mmproj" in notes:
        advice.append(
            Advice(
                issue="projector_missing",
                severity="warning",
                detail="The backend rejected an image: the multimodal projector "
                "(mmproj) is not loaded.",
                fix="llama.cpp needs --mmproj pointing at the projector from the "
                "model repo. LMDS picks it up automatically when it generates the "
                "bundle; a hand-written unit has to pass it.",
                command="./<controller>.sh restart   # after adding --mmproj",
            )
        )

    if any("--reasoning-parser" in note for note in result.notes):
        advice.append(
            Advice(
                issue="reasoning_not_separated",
                severity="info",
                detail="The model narrates its reasoning inside the answer: the "
                "server was started without a --reasoning-parser.",
                fix="Restart with the parser for this model family, so the chain "
                "of thought arrives in reasoning_content and the answer stays "
                "clean. Chat surfaces otherwise have to strip it by guesswork.",
                command="./<controller>.sh restart --reasoning-parser <parser>",
            )
        )

    if result.context_tokens is None:
        advice.append(
            Advice(
                issue="context_unknown",
                severity="info",
                detail="The backend did not report its context window.",
                fix="Read it from the server rather than the launch flags: "
                "llama.cpp divides --ctx-size by --parallel, so the per-request "
                "window is smaller than the flag suggests.",
                command="curl -s <base_url>/props | jq .default_generation_settings.n_ctx",
            )
        )

    if result.capabilities.get("tools") and not result.protocols.get("anthropic"):
        advice.append(
            Advice(
                issue="anthropic_via_translation",
                severity="info",
                detail="The backend speaks OpenAI only, but has tool calling.",
                fix="Enable the Anthropic surface on the alias - the gateway "
                "translates, so Claude Code can use this model as it is.",
            )
        )

    return advice


async def probe_backend(
    base_url: str, upstream_model: str = "", api_key: str = "", timeout: float = 60.0
) -> ProbeResult:
    """Ask a model server what it can actually do.

    The result is a *suggestion*: the admin must confirm before it is saved
    (PRD FR-39). Nothing here writes to the registry.
    """
    base_url = base_url.rstrip("/")
    result = ProbeResult()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        # 1. What is served?
        try:
            response = await client.get(f"{base_url}/v1/models")
            response.raise_for_status()
            data = response.json().get("data") or []
            result.reachable = True
            result.served_models = [m.get("id", "") for m in data if m.get("id")]
            chosen = None
            for entry in data:
                if upstream_model and entry.get("id") == upstream_model:
                    chosen = entry
                    break
            chosen = chosen or (data[0] if data else None)
            if chosen:
                result.upstream_model = upstream_model or chosen.get("id", "")
                for key in ("max_model_len", "context_length", "n_ctx"):
                    if isinstance(chosen.get(key), int):
                        result.context_tokens = chosen[key]
                        break
        except Exception as exc:
            result.notes.append(f"could not read /v1/models: {type(exc).__name__}: {exc}")
            return result

        # llama.cpp reports the real window on /props, not on /v1/models.
        if result.context_tokens is None:
            try:
                response = await client.get(f"{base_url}/props")
                n_ctx = (response.json().get("default_generation_settings") or {}).get("n_ctx")
                if isinstance(n_ctx, int):
                    result.context_tokens = n_ctx
            except Exception:
                result.notes.append("context window unknown; set it manually")

        model = result.upstream_model
        chat_url = f"{base_url}/v1/chat/completions"

        async def try_chat(payload: dict[str, Any]) -> httpx.Response | None:
            try:
                return await client.post(chat_url, json=payload)
            except Exception as exc:
                result.notes.append(f"request failed: {type(exc).__name__}: {exc}")
                return None

        # 2. chat
        response = await try_chat(
            {"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 16}
        )
        result.capabilities["chat"] = bool(response is not None and response.status_code == 200)
        if response is not None and response.status_code != 200:
            result.notes.append(f"chat -> HTTP {response.status_code}")

        # 3. streaming
        streaming = False
        try:
            async with client.stream(
                "POST",
                chat_url,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 16,
                    "stream": True,
                },
            ) as stream_response:
                if stream_response.status_code == 200:
                    async for line in stream_response.aiter_lines():
                        if line.startswith("data:"):
                            streaming = True
                            break
        except Exception:
            pass
        result.capabilities["streaming"] = streaming

        # 3b. A reasoning model started without --reasoning-parser puts its
        #     chain of thought in `content`. Whatever consumes the answer then
        #     has to strip it by guesswork, which is as fragile as it sounds -
        #     the gateway's own chat panel hit exactly this. Cheap to notice
        #     here: reuse the chat reply, look at where the thinking went.
        if response is not None and response.status_code == 200:
            try:
                message = response.json()["choices"][0]["message"]
                # vLLM has used both names: `reasoning_content` in older builds,
                # `reasoning` in newer ones. Checking only one reports a working
                # --reasoning-parser as missing, which sends people to fix
                # something that is already right.
                separated = any(
                    message.get(field) is not None
                    for field in ("reasoning_content", "reasoning")
                )
                text = (message.get("content") or "")[:400]
                narrates = any(
                    marker.lower() in text.lower()
                    # `</think>` alone is the common case: Qwen3 and DeepSeek-R1
                    # templates prefill the opening tag, so only the close leaks.
                    for marker in ("</think>", "Thinking Process", "Let me think", "Final Answer:")
                )
                result.capabilities["reasoning_separated"] = separated
                if narrates and not separated:
                    result.notes.append("reasoning inside content: no --reasoning-parser")
            except Exception:  # a backend that answers 200 with something else
                pass

        # 4. tools - 200 alone is not enough; the backend must emit tool_calls.
        response = await try_chat(
            {
                "model": model,
                "messages": [{"role": "user", "content": "What is the weather in Bangkok?"}],
                "max_tokens": 128,
                "tools": [
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
            }
        )
        tools = False
        if response is not None and response.status_code == 200:
            try:
                message = response.json()["choices"][0]["message"]
                tools = bool(message.get("tool_calls"))
            except Exception:
                tools = False
            if not tools:
                result.notes.append(
                    "tools: backend accepted the request but returned no tool_calls "
                    "(vLLM needs --enable-auto-tool-choice with a --tool-call-parser)"
                )
        elif response is not None:
            # A rejected tools request usually says exactly what the backend is
            # missing - that message is the most actionable thing an admin can
            # get here, so surface it rather than just recording tools=false.
            result.notes.append(
                f"tools -> HTTP {response.status_code}: {_short_error(response)}"
            )
        result.capabilities["tools"] = tools

        # 5. vision - a vision-capable architecture may still be served without
        #    the projector, so this must be measured, never inferred (PRD §9.5).
        response = await try_chat(
            {
                "model": model,
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What colour is the diagonal line?"},
                            {"type": "image_url", "image_url": {"url": png_data_url()}},
                        ],
                    }
                ],
            }
        )
        vision = bool(response is not None and response.status_code == 200)
        result.capabilities["vision"] = vision
        if response is not None and response.status_code != 200:
            result.notes.append(
                f"vision -> HTTP {response.status_code}: {_short_error(response)}"
            )

        # 6. native Anthropic surface?
        anthropic = False
        try:
            response = await client.post(
                f"{base_url}/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            anthropic = response.status_code == 200
        except Exception:
            anthropic = False
        result.protocols = {"openai": result.capabilities["chat"], "anthropic": anthropic}

    result.server_kind = _detect_server_kind(result.notes, result.served_models)
    result.advice = build_advice(result)
    return result
