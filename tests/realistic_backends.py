"""Backend ปลอมที่ทำตัวเหมือนของจริง — ใช้ร่วมกันในเทสของวงจร stream

ทำไมต้องมีไฟล์นี้ (ตรวจ 2026-10-06): บั๊กเรื่องเงินทั้งชุดผ่านชุดเทสเดิมไปได้เพราะ mock
ของเทสเดิม **ใจดีกว่า backend จริง** สองอย่าง

1. แนบ usage chunk มาเสมอ ไม่ว่าคำขอจะขอหรือไม่ — vLLM จริงส่งเฉพาะเมื่อคำขอมี
   `stream_options.include_usage` เทสจึงเขียวทั้งที่ /v1/messages กับ /v1/responses
   ไม่เคยขอ และ output ถูกคิดเป็น 0 ทุกคำขอบนเครื่องจริง
2. ส่ง stream จนจบทุกครั้ง — ไม่เคยตายกลางทาง ไม่เคยเงียบจนหมดเวลา ไม่เคยใส่ error object
   มาใน stream ที่ตอบ 200 ไปแล้ว และผู้เรียกในเทสไม่เคยตัดสาย

ของในไฟล์นี้ทำทั้งหมดนั้น และขับแอปผ่าน ASGI แบบเดียวกับ uvicorn (`receive()` คืน
`http.disconnect` กลาง stream) เพราะ `TestClient` อ่านจนจบเสมอ
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Any

import httpx
import yaml

CODING = "http://dgx03:8000"   # coding · vllm · พูดแต่ chat completions · 16 ช่อง
MUSE = "http://dgx01:8000"     # muse-local · llama.cpp · พูด Anthropic เองด้วย · 1 ช่อง
SPARE = "http://dgx-spare:8000"

MESSAGES = [{"role": "user", "content": "write a long answer"}]

# (path, ส่วนของ body ที่ต่างกัน) ของทั้งสาม surface — คำขอเดียวกันในสามภาษา
SURFACES: dict[str, tuple[str, dict[str, Any]]] = {
    "chat": ("/v1/chat/completions", {"messages": MESSAGES}),
    "messages": ("/v1/messages", {"messages": MESSAGES, "max_tokens": 256}),
    "responses": ("/v1/responses", {"input": "write a long answer"}),
}
SURFACE_NAMES = list(SURFACES)


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def request_for(surface: str, model: str = "coding", **extra: Any) -> tuple[str, dict[str, Any]]:
    path, body = SURFACES[surface]
    return path, {"model": model, **body, **extra}


def edit(config, alias: str, change) -> None:  # noqa: ANN001
    """แก้ YAML ของโมเดลใน config ชั่วคราว (ต้องใช้คู่กับ fixture `writable_config`)"""
    path = config / "models" / f"{alias}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


def add_spare(config, alias: str = "coding") -> None:  # noqa: ANN001
    """เพิ่มเครื่องสำรอง (priority ต่ำกว่า) ให้ alias"""

    def change(document: dict) -> None:
        first = document["spec"]["endpoints"][0]
        document["spec"]["endpoints"] = [
            first, {**first, "name": "spare", "base_url": SPARE, "priority": 90},
        ]

    edit(config, alias, change)


# ── สิ่งที่ backend ส่ง ────────────────────────────────────────────────────────
def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "upstream-name",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def sse(payload: dict[str, Any] | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


ROLE = sse(chunk({"role": "assistant", "content": ""}))
STOP = sse(chunk({}, "stop"))
DONE = b"data: [DONE]\n\n"


def words(count: int, start: int = 0) -> list[bytes]:
    return [sse(chunk({"content": f"word{i} "})) for i in range(start, start + count)]


def usage_chunk(prompt: int, completion: int) -> bytes:
    return sse({"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                          "total_tokens": prompt + completion}})


class Script(httpx.AsyncByteStream):
    """body ของ stream ที่เดินตามบท: bytes = ส่ง · ตัวเลข = รอ (วินาที) · Exception = สายพัง"""

    def __init__(self, *steps: bytes | float | Exception) -> None:
        self.steps = steps
        self.sent = 0
        self.closed = False

    async def __aiter__(self):
        for step in self.steps:
            if isinstance(step, Exception):
                raise step
            if isinstance(step, (int, float)):
                await asyncio.sleep(step)
                continue
            self.sent += 1
            yield step

    async def aclose(self) -> None:
        self.closed = True


def streaming(*steps: bytes | float | Exception, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "text/event-stream"}, stream=Script(*steps)
    )


class VllmLike:
    """side_effect ของ respx ที่ตอบ chat completions แบบ vLLM

    **usage chunk ปิดท้ายมาเฉพาะเมื่อคำขอขอ** (`stream_options.include_usage`) — นี่คือ
    ข้อเดียวที่ mock เดิมทำผิดและทำให้บั๊ก output=0 ผ่านเทสมาได้ · `seen` เก็บ body ของทุก
    คำขอที่มาถึง
    """

    def __init__(self, pieces: int = 40, *, prompt_tokens: int = 1234,
                 completion_tokens: int = 321) -> None:
        self.pieces = pieces
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.seen: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append(body)
        if not body.get("stream"):
            return httpx.Response(200, json={
                "id": "chatcmpl-1", "object": "chat.completion", "model": "upstream-name",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant",
                    "content": "".join(f"word{i} " for i in range(self.pieces))}}],
                "usage": {"prompt_tokens": self.prompt_tokens,
                          "completion_tokens": self.completion_tokens,
                          "total_tokens": self.prompt_tokens + self.completion_tokens},
            })
        steps = [ROLE, *words(self.pieces), STOP]
        if (body.get("stream_options") or {}).get("include_usage"):
            steps.append(usage_chunk(self.prompt_tokens, self.completion_tokens))
        return streaming(*steps, DONE)


# ── สิ่งที่ผู้เรียกเห็น ────────────────────────────────────────────────────────
def sse_events(raw: str) -> list[tuple[str | None, Any]]:
    """body ของ SSE เป็นคู่ (event, payload ที่ parse แล้ว หรือสตริงดิบ)"""
    out: list[tuple[str | None, Any]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        event, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if not data:
            continue
        payload = "\n".join(data)
        try:
            out.append((event, json.loads(payload)))
        except json.JSONDecodeError:
            out.append((event, payload))
    return out


def read_stream(client, key: str, surface: str, model: str = "coding", **extra: Any):  # noqa: ANN001
    """ยิงคำขอแบบ stream แล้วอ่านจนจบ — คืน (response, events)"""
    path, body = request_for(surface, model, stream=True, **extra)
    with client.stream("POST", path, headers=auth(key), json=body) as response:
        raw = response.read().decode("utf-8")
    return response, sse_events(raw)


def text_of(surface: str, events: list[tuple[str | None, Any]]) -> str:
    if surface == "chat":
        return "".join(
            (p["choices"][0]["delta"].get("content") or "")
            for _, p in events if isinstance(p, dict) and p.get("choices")
        )
    if surface == "messages":
        return "".join(
            p["delta"]["text"] for e, p in events
            if e == "content_block_delta" and p["delta"]["type"] == "text_delta"
        )
    return "".join(p["delta"] for e, p in events if e == "response.output_text.delta")


def terminal_error(surface: str, events: list[tuple[str | None, Any]]) -> dict[str, Any] | None:
    """event ปิดท้ายที่บอกว่าคำตอบไม่จบ ในรูปของ surface นั้น — None ถ้าไม่มี

    chat: chunk ที่มี `error` (สิ่งที่ SDK ของ OpenAI ยกเป็น APIError) ตามด้วย `[DONE]`
    messages: event `error` ตามสเปก streaming ของ Anthropic
    responses: `response.failed`
    """
    if surface == "chat":
        errors = [p for _, p in events if isinstance(p, dict) and p.get("error")]
        if not errors or events[-1][1] != "[DONE]":
            return None
        return errors[-1]["error"]
    if surface == "messages":
        event, payload = events[-1]
        if event != "error" or payload.get("type") != "error":
            return None
        return payload["error"]
    event, payload = events[-1]
    if event != "response.failed" or payload["response"]["status"] != "failed":
        return None
    return payload["response"]["error"]


def ended_normally(surface: str, events: list[tuple[str | None, Any]]) -> bool:
    names = [e for e, _ in events]
    if surface == "chat":
        return terminal_error(surface, events) is None and events[-1][1] == "[DONE]"
    if surface == "messages":
        return names[-1] == "message_stop"
    return names[-1] in ("response.completed", "response.incomplete")


# ── สิ่งที่ถูกบันทึก ────────────────────────────────────────────────────────────
def settle(client) -> None:  # noqa: ANN001
    """รอให้งานเบื้องหลังของคำขอที่เพิ่งจบ (บันทึก usage · คืนช่อง) เสร็จ"""
    from app.api.openai import drain_pending_finalizers

    services = client.app.state.services

    async def drain() -> None:
        await drain_pending_finalizers(timeout=5.0)
        await services.router.drain_releases()
        await services.usage.flush()

    client.portal.call(drain)


def usage_rows(client) -> list[dict[str, Any]]:  # noqa: ANN001
    """ทุกแถวใน usage_logs เรียงตามเวลา"""
    from sqlalchemy import select

    from app.db.models import UsageLog
    from app.db.session import session_scope

    settle(client)

    async def read() -> list[dict[str, Any]]:
        async with session_scope() as session:
            rows = (await session.execute(select(UsageLog).order_by(UsageLog.ts))).scalars().all()
            return [
                {
                    "protocol": r.protocol,
                    "endpoint": r.endpoint_name,
                    "status": r.status,
                    "http_status": r.http_status,
                    "error_code": r.error_code,
                    "input": r.text_input_tokens + r.visual_input_tokens,
                    "output": r.output_tokens,
                    "accounting": r.token_accounting,
                    "stream": r.stream,
                }
                for r in rows
            ]

    return client.portal.call(read)


def quota_used(client) -> dict[str, int]:  # noqa: ANN001
    """ตัวนับโควตาของสมาชิกจาก fixture `member_key`"""
    settle(client)
    data = client.get("/admin/usage/quota", headers=auth(client.admin_key)).json()["data"]
    return next(r for r in data if r["external_id"] == "6412345678")["used"]


def endpoint_health(client, key: str = "coding:dgx03") -> dict[str, Any]:  # noqa: ANN001
    data = client.get("/v1/health/endpoints", headers=auth(client.admin_key)).json()["data"]
    return data[key]


def slot_state(client, alias: str, index: int = 0) -> tuple[int, int]:  # noqa: ANN001
    """(คำใบ้ในเครื่องของ process นี้, จำนวนใบจองในตัวนับจริง) ของ endpoint หนึ่ง"""
    settle(client)
    services = client.app.state.services
    endpoint = services.registry.snapshot.models[alias].spec.endpoints[index]
    slot = services.router._slot(alias, endpoint)
    held = client.portal.call(services.router._limiter.count, slot)
    return services.router.in_flight(alias, endpoint), held


def another_worker_holds(client, alias: str, index: int = 0) -> None:  # noqa: ANN001
    """จองช่องทั้งหมดของ endpoint แบบที่ gateway worker อีกตัวทำผ่านตัวนับร่วม

    ใบจองอยู่ในตัวนับจริง แต่คำใบ้ `_in_flight` ของ process นี้ยังเป็น 0 — `select()` จึง
    มองว่าว่าง และคำขอไปชนที่ `acquire()` · นี่คือทุก deployment ที่มีหลาย worker
    """
    services = client.app.state.services
    endpoint = services.registry.snapshot.models[alias].spec.endpoints[index]
    slot = services.router._slot(alias, endpoint)

    async def take() -> None:
        for n in range(endpoint.max_concurrency):
            assert await services.router._limiter.acquire(
                slot, endpoint.max_concurrency, f"lease-of-another-worker-{n}"
            )

    client.portal.call(take)
    assert services.router.in_flight(alias, endpoint) == 0


# ── ผู้เรียกที่ตัดสาย ──────────────────────────────────────────────────────────
async def hang_up(app, key: str, path: str, body: dict[str, Any], *,  # noqa: ANN001
                  after_frames: int = 1, linger: float = 0.05) -> dict[str, Any]:
    """ขับแอปผ่าน ASGI แบบ uvicorn แล้วตัดสายหลังได้ body ครบ `after_frames` frame

    `after_frames=0` = ตัดสายระหว่างรอ token แรก (ก่อน response เริ่ม) · uvicorn บอกแอปว่า
    ผู้เรียกไปแล้วด้วยการให้ `receive()` คืน `http.disconnect` แล้ว Starlette ยกเลิก task
    ของ stream ผ่าน cancel scope ของ anyio — เส้นทางที่ `TestClient` ไม่เคยเดิน
    """
    payload = json.dumps(body).encode()
    seen: dict[str, Any] = {"status": None, "headers": {}, "frames": 0, "raised": None,
                            "body": b""}
    enough = asyncio.Event()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        if after_frames > 0:
            await enough.wait()
        await asyncio.sleep(linger)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]
            seen["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body" and message.get("body"):
            seen["frames"] += 1
            seen["body"] += message["body"]
            if seen["frames"] >= after_frames:
                enough.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "client": ("127.0.0.1", 50000), "server": ("gateway", 80), "state": {},
        "headers": [(b"host", b"gateway"), (b"content-type", b"application/json"),
                    (b"authorization", f"Bearer {key}".encode()),
                    (b"content-length", str(len(payload)).encode())],
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=15)
    except BaseException as exc:  # noqa: BLE001 - อะไรก็ตามที่ uvicorn จะกลืนไว้
        seen["raised"] = repr(exc)
    return seen


def hang_up_on(client, key: str, surface: str, model: str = "coding", *,  # noqa: ANN001
               after_frames: int = 1, **extra: Any) -> dict[str, Any]:
    """`hang_up` จากเทสแบบ sync: คำขอ stream ของ surface นั้น แล้วตัดสาย"""
    path, body = request_for(surface, model, stream=True, **extra)
    return client.portal.call(
        functools.partial(hang_up, client.app, key, path, body, after_frames=after_frames)
    )
