"""header ที่ไปถึง backend เป็นของเกตเวย์ ไม่ใช่ของ client ที่ส่งผ่านมา

ตรวจ 2026-10-06: `upstream_headers` ตัดแค่ hop-by-hop กับ credential ที่เหลือส่งต่อทั้งหมด

    accept-encoding: br, zstd     → backend ที่ทำตาม (คลาวด์หลัง CDN) ตอบ brotli กลับมา ·
                                    httpx ของเกตเวย์ถอดได้แค่ gzip/deflate — อ่านไม่ออก และ
                                    ไบต์ที่ไม่ใช่ UTF-8 ทำให้ json โยน UnicodeDecodeError ซึ่ง
                                    ไม่ใช่ JSONDecodeError ที่โค้ดดักไว้
    x-forwarded-for · openai-organization · x-stainless-os …
                                  → IP · รหัสองค์กร · OS ของเครื่องสมาชิก ไปถึง backend ซึ่ง
                                    อาจเป็นผู้ให้บริการภายนอก

เทสดู header ที่ backend **ได้รับจริง** บนทุก surface ที่ส่งต่อคำขอ
"""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml

CODING = "http://dgx03:8000"
MUSE = "http://dgx01:8000"
EMBED = "http://dgx05:8000"

CHAT_REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
NATIVE_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "up",
    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 2},
}
EMBED_REPLY = {"object": "list", "model": "up",
               "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
               "usage": {"prompt_tokens": 1, "total_tokens": 1}}
STREAM = (b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
          b"data: [DONE]\n\n")
MSG = [{"role": "user", "content": "hi"}]

# สิ่งที่ SDK ของ OpenAI บน Node ส่งมาจริง + สิ่งที่ reverse proxy หน้าเกตเวย์เติมให้
CLIENT_ONLY = {
    "accept-encoding": "br, zstd",
    "x-forwarded-for": "10.1.2.3",
    "x-forwarded-proto": "https",
    "x-forwarded-host": "gw.school.example",
    "x-real-ip": "10.1.2.3",
    "forwarded": "for=10.1.2.3;proto=https",
    "openai-organization": "org-secret-123",
    "openai-project": "proj-secret-456",
    "x-stainless-os": "MacOS",
    "x-stainless-runtime-version": "v22.1.0",
    "x-stainless-arch": "arm64",
    "cookie": "session=abc",
}
# สิ่งที่ backend ต้องได้ต่อไป — ตัดเกินก็พังเหมือนกัน
STILL_RELAYED = {
    "anthropic-beta": "context-1m-2025-08-07",
    "anthropic-version": "2023-06-01",
    "user-agent": "OpenAI/JS 5.1.0",
}

SURFACES = {
    "chat": ("/v1/chat/completions", {"model": "coding", "messages": MSG},
             f"{CODING}/v1/chat/completions", CHAT_REPLY),
    "chat-stream": ("/v1/chat/completions", {"model": "coding", "messages": MSG, "stream": True},
                    f"{CODING}/v1/chat/completions", None),
    "messages-translated": ("/v1/messages", {"model": "coding", "max_tokens": 8,
                                             "messages": MSG},
                            f"{CODING}/v1/chat/completions", CHAT_REPLY),
    "messages-native": ("/v1/messages", {"model": "muse-local", "max_tokens": 8,
                                         "messages": MSG},
                        f"{MUSE}/v1/messages", NATIVE_REPLY),
    "responses": ("/v1/responses", {"model": "coding", "input": "hi"},
                  f"{CODING}/v1/chat/completions", CHAT_REPLY),
    "embeddings": ("/v1/embeddings", {"model": "embed", "input": "hi"},
                   f"{EMBED}/v1/embeddings", EMBED_REPLY),
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def with_embeddings(writable_config):
    (writable_config / "models" / "embed.yaml").write_text(yaml.safe_dump({
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": "embed", "display_name": "Embed", "visibility": "member"},
        "spec": {"upstream_model": "org/embed", "purpose": ["embedding"],
                 "limits": {"context_tokens": 8192, "max_output_tokens": 16},
                 "capabilities": {"chat": False, "streaming": False, "embedding": True},
                 "protocols": {"openai": False, "embeddings": True},
                 "endpoints": [{"name": "e1", "server_type": "vllm", "base_url": EMBED,
                                "protocols": {"openai": False, "embeddings": True}}]}}))
    return writable_config


@pytest.mark.parametrize("surface", SURFACES)
def test_client_only_headers_do_not_reach_the_backend(
        with_embeddings, client, member_key, surface):
    path, body, upstream_url, reply = SURFACES[surface]
    with respx.mock:
        route = respx.post(upstream_url).mock(return_value=(
            httpx.Response(200, json=reply) if reply is not None else
            httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})))
        response = client.post(path, json=body, headers={
            **auth(member_key), **CLIENT_ONLY, **STILL_RELAYED})
        response.read()
        assert response.status_code == 200, response.text
        seen = route.calls.last.request.headers

    # การบีบอัดที่ขอ ต้องเป็นของที่ httpx ตัวนี้ถอดได้จริงเท่านั้น
    offered = {part.strip() for part in seen.get("accept-encoding", "").split(",") if part.strip()}
    from httpx._decoders import SUPPORTED_DECODERS

    assert offered <= set(SUPPORTED_DECODERS), offered
    assert "zstd" not in offered or "zstd" in SUPPORTED_DECODERS

    for name, value in CLIENT_ONLY.items():
        if name == "accept-encoding":
            continue
        assert name not in seen, f"{name} ไปถึง backend"
        assert value not in " ".join(seen.values())
    assert "authorization" not in seen or member_key not in seen["authorization"], (
        "API key ของสมาชิกต้องไม่ไปถึง backend")
    for name, value in STILL_RELAYED.items():
        assert seen.get(name) == value, f"{name} ต้องยังถูกส่งต่อ"


@pytest.mark.parametrize("surface", ["chat", "messages-translated", "responses", "embeddings"])
def test_a_body_that_is_not_utf8_is_a_502_not_a_500(
        with_embeddings, client, member_key, surface):
    """ไบต์ที่ยังบีบอัดอยู่ (หรือไฟล์ไบนารีจาก proxy ที่คั่นอยู่) — json โยน UnicodeDecodeError"""
    path, body, upstream_url, _ = SURFACES[surface]
    garbage = b"\x1b\x0e\x00\xf8\x8d\x94\x6e\xde\x44\x55\x86\x96\x20\x9b\x0f\xff\xfe"
    with respx.mock:
        respx.post(upstream_url).mock(return_value=httpx.Response(
            200, content=garbage, headers={"content-type": "application/json"}))
        response = client.post(path, json=body, headers=auth(member_key))

    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"
