"""End-to-end request path with the upstream model server mocked out."""

from __future__ import annotations

import json

import httpx
import respx

from tests.conftest import png_data_url

UPSTREAM_CHAT = "http://dgx03:8000/v1/chat/completions"
UPSTREAM_VISION = "http://dgx02:8000/v1/chat/completions"

OPENAI_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "ucbye/Qwen3-Coder-Next-NVFP4-GB10",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "print('hello')"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_missing_key_is_401(client):
    response = client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MISSING_API_KEY"


def test_invalid_key_is_401(client):
    response = client.get("/v1/models", headers=auth("edu_sk_nope"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


def test_x_api_key_header_is_accepted(client, member_key):
    response = client.get("/v1/models", headers={"x-api-key": member_key})
    assert response.status_code == 200


def test_revoked_key_is_rejected(client, member_key):
    keys = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    key_id = next(k["id"] for k in keys if not k["revoked"])
    client.delete(f"/admin/api-keys/{key_id}", headers=auth(client.admin_key))
    response = client.get("/v1/models", headers=auth(member_key))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "API_KEY_REVOKED"


# ---------------------------------------------------------------------------
# Catalogue / model naming (PRD §6)
# ---------------------------------------------------------------------------
def test_member_never_sees_upstream_model_name(client, member_key):
    payload = client.get("/v1/models", headers=auth(member_key)).json()
    body = json.dumps(payload)
    assert "Qwen3-Coder" not in body
    assert "gemma-4-31B" not in body
    assert "Muse-Glimmer" not in body
    assert {"coding", "gemma-vision", "muse-local"} == {m["id"] for m in payload["data"]}


def test_admin_sees_upstream_model_name(client):
    payload = client.get("/v1/models", headers=auth(client.admin_key)).json()
    entry = next(m for m in payload["data"] if m["id"] == "coding")
    assert entry["upstream_model"] == "ucbye/Qwen3-Coder-Next-NVFP4-GB10"


def test_catalog_groups_by_purpose(client, member_key):
    payload = client.get("/v1/catalog", headers=auth(member_key)).json()
    titles = {s["title"] for s in payload["sections"]}
    assert {"General AI", "Vision AI", "Coding AI"} <= titles

    coding_section = next(s for s in payload["sections"] if s["purpose"] == "coding")
    coding = coding_section["models"][0]
    assert coding["id"] == "coding"
    assert coding["claude_code_ready"] is True
    assert "Code" in coding["badges"] and "Agent" in coding["badges"]
    # Descriptions may name a *client* ("Qwen Code"); what must never leak is the
    # upstream repository path.
    assert "ucbye/" not in json.dumps(payload)
    assert "Qwen3-Coder-Next" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Capability validation (PRD §4)
# ---------------------------------------------------------------------------
def test_image_to_text_only_model_returns_400(client, member_key):
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={
            "model": "coding",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "อธิบายภาพนี้"},
                        {"type": "image_url", "image_url": {"url": png_data_url()}},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "MODEL_CAPABILITY_NOT_SUPPORTED"
    assert "does not support image input" in error["message"]


def test_unknown_model_returns_404_with_alternatives(client, member_key):
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "gpt-9", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "MODEL_NOT_FOUND"
    assert "coding" in error["details"]["available_models"]


def test_anthropic_surface_rejects_model_without_that_protocol(client, member_key):
    """gemma-vision declares protocols.anthropic=false, so /v1/messages must 400.

    This used to point at muse-local, until that backend moved to llama.cpp and
    started answering /v1/messages itself. Any member-visible model with
    anthropic=false does the job — the subject here is the gateway's refusal,
    not the model.
    """
    response = client.post(
        "/v1/messages",
        headers=auth(member_key),
        json={
            "model": "gemma-vision",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROTOCOL_NOT_SUPPORTED"
    assert response.json()["type"] == "error"  # Anthropic envelope


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------
@respx.mock
def test_chat_completion_is_forwarded_and_alias_is_restored(client, member_key):
    route = respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(200, json=OPENAI_REPLY)
    )
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    body = response.json()
    # The response must name the alias, not the upstream repository.
    assert body["model"] == "coding"
    assert body["choices"][0]["message"]["content"] == "print('hello')"
    assert body["usage"]["litegate"]["accounting"] == "upstream"

    # The upstream received the real model name.
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "ucbye/Qwen3-Coder-Next-NVFP4-GB10"
    # The member's gateway key must never be forwarded upstream.
    assert member_key not in route.calls[0].request.headers.get("authorization", "")


@respx.mock
def test_vision_request_reaches_vision_backend(client, member_key):
    route = respx.post(UPSTREAM_VISION).mock(
        return_value=httpx.Response(200, json={**OPENAI_REPLY, "model": "google/gemma-4-31B-it"})
    )
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={
            "model": "gemma-vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "อธิบายภาพนี้"},
                        {"type": "image_url", "image_url": {"url": png_data_url(512, 512)}},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gemma-vision"
    assert response.json()["usage"]["litegate"]["visual_input_tokens"] > 0

    # PRD §13: content blocks are forwarded untouched, not flattened or re-encoded.
    sent = json.loads(route.calls[0].request.content)
    blocks = sent["messages"][0]["content"]
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


@respx.mock
def test_upstream_500_becomes_502_with_gateway_envelope(client, member_key):
    respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(500, text="CUDA out of memory")
    )
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert error["details"]["upstream_status"] == 500


@respx.mock
def test_streaming_passthrough(client, member_key):
    chunks = [
        'data: {"id":"1","choices":[{"delta":{"content":"he"},"index":0}]}\n\n',
        'data: {"id":"1","choices":[{"delta":{"content":"llo"},"index":0}]}\n\n',
        'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n',
        'data: {"id":"1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]
    respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(
            200, text="".join(chunks), headers={"content-type": "text/event-stream"}
        )
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth(member_key),
        json={
            "model": "coding",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payload = "".join(response.iter_text())

    assert "he" in payload and "llo" in payload
    assert payload.rstrip().endswith("data: [DONE]")
    # The caller did not ask for usage, so the usage-only chunk is stripped.
    assert "prompt_tokens" not in payload
    # Every forwarded chunk carries the alias.
    assert '"model": "coding"' in payload or '"model":"coding"' in payload


@respx.mock
def test_streaming_usage_is_kept_when_client_requests_it(client, member_key):
    chunks = [
        'data: {"id":"1","choices":[{"delta":{"content":"hi"},"index":0}]}\n\n',
        'data: {"id":"1","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]
    respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(
            200, text="".join(chunks), headers={"content-type": "text/event-stream"}
        )
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth(member_key),
        json={
            "model": "coding",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as response:
        payload = "".join(response.iter_text())
    assert "prompt_tokens" in payload


# ---------------------------------------------------------------------------
# Anthropic surface (Claude Code)
# ---------------------------------------------------------------------------
@respx.mock
def test_anthropic_messages_translated_to_openai(client, member_key):
    route = respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(200, json=OPENAI_REPLY)
    )
    response = client.post(
        "/v1/messages",
        headers=auth(member_key),
        json={
            "model": "coding",
            "max_tokens": 64,
            "system": "You are a helpful coding assistant.",
            "messages": [{"role": "user", "content": "write hello world"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "coding"
    assert body["content"][0]["type"] == "text"
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 12

    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0] == {
        "role": "system",
        "content": "You are a helpful coding assistant.",
    }
    assert sent["max_tokens"] == 64


@respx.mock
def test_anthropic_tool_definitions_are_translated(client, member_key):
    route = respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                **OPENAI_REPLY,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"main.py"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )
    )
    response = client.post(
        "/v1/messages",
        headers=auth(member_key),
        json={
            "model": "coding",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "read main.py"}],
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
    assert response.status_code == 200
    body = response.json()
    assert body["stop_reason"] == "tool_use"
    tool_use = next(b for b in body["content"] if b["type"] == "tool_use")
    assert tool_use["name"] == "read_file"
    assert tool_use["input"] == {"path": "main.py"}

    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["function"]["name"] == "read_file"
    assert sent["tools"][0]["function"]["parameters"]["properties"]["path"]


@respx.mock
def test_anthropic_streaming_event_sequence(client, member_key):
    chunks = [
        'data: {"id":"1","choices":[{"delta":{"content":"Hel"},"index":0}]}\n\n',
        'data: {"id":"1","choices":[{"delta":{"content":"lo"},"index":0}]}\n\n',
        'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop","index":0}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]
    respx.post(UPSTREAM_CHAT).mock(
        return_value=httpx.Response(
            200, text="".join(chunks), headers={"content-type": "text/event-stream"}
        )
    )
    with client.stream(
        "POST",
        "/v1/messages",
        headers=auth(member_key),
        json={
            "model": "coding",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        payload = "".join(response.iter_text())

    events = [line[7:] for line in payload.splitlines() if line.startswith("event: ")]
    assert events[0] == "message_start"
    assert "content_block_start" in events
    assert "content_block_delta" in events
    assert events[-2:] == ["message_delta", "message_stop"]
    assert "Hel" in payload and "lo" in payload


def test_count_tokens_endpoint(client, member_key):
    response = client.post(
        "/v1/messages/count_tokens",
        headers=auth(member_key),
        json={
            "model": "coding",
            "messages": [{"role": "user", "content": "x" * 320}],
        },
    )
    assert response.status_code == 200
    assert response.json()["input_tokens"] == 100


# ---------------------------------------------------------------------------
# Quota (PRD §10)
# ---------------------------------------------------------------------------
@respx.mock
def test_quota_exhaustion_returns_429(client, member_key):
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))

    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    member = next(u for u in users if u["external_id"] == "6412345678")
    client.post(
        "/admin/quota-policies",
        headers=auth(client.admin_key),
        json={
            "scope": "user",
            "user_id": member["id"],
            "window": "day",
            "max_requests": 1,
        },
    )

    first = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert first.status_code == 200

    second = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "QUOTA_EXCEEDED"
    assert error["details"]["limit"] == 1
    assert "retry-after" in second.headers


@respx.mock
def test_usage_is_recorded_without_prompt_content(client, member_key):
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    secret = "MY SECRET PROMPT 12345"
    client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": secret}]},
    )
    summary = client.get(
        "/admin/usage/summary?days=1", headers=auth(client.admin_key)
    ).json()
    assert secret not in json.dumps(summary)
    coding = next(m for m in summary["by_model"] if m["model"] == "coding")
    assert coding["requests"] >= 1
    assert coding["output_tokens"] == 5


# ---------------------------------------------------------------------------
# Workspace policy
# ---------------------------------------------------------------------------
def test_workspace_bound_key_cannot_use_unlisted_model(client):
    headers = auth(client.admin_key)
    workspace = client.post(
        "/admin/workspaces",
        json={"code": "CS101", "name": "Intro", "term": "1/2569"},
        headers=headers,
    ).json()
    client.post(
        f"/admin/workspaces/{workspace['id']}/models",
        json={"models": ["coding"]},
        headers=headers,
    )
    user = client.post(
        "/admin/users",
        json={"external_id": "6499999999", "display_name": "Ploy", "role": "member"},
        headers=headers,
    ).json()
    key = client.post(
        "/admin/api-keys",
        json={"user_id": user["id"], "workspace_id": workspace["id"]},
        headers=headers,
    ).json()["api_key"]

    denied = client.post(
        "/v1/chat/completions",
        headers=auth(key),
        json={"model": "gemma-vision", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "MODEL_NOT_PERMITTED"


def test_setting_unknown_alias_on_a_workspace_is_rejected(client):
    headers = auth(client.admin_key)
    workspace = client.post(
        "/admin/workspaces",
        json={"code": "CS999", "name": "Ghost", "term": "1/2569"},
        headers=headers,
    ).json()
    response = client.post(
        f"/admin/workspaces/{workspace['id']}/models",
        json={"models": ["does-not-exist"]},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_member_cannot_reach_admin_plane(client, member_key):
    assert client.get("/admin/models", headers=auth(member_key)).status_code == 403
    assert client.get("/admin/users", headers=auth(member_key)).status_code == 403


def test_me_reports_quota(client, member_key):
    payload = client.get("/v1/me", headers=auth(member_key)).json()
    assert payload["external_id"] == "6412345678"
    assert payload["quota"]["window"] == "day"
    assert "max_requests" in payload["quota"]["limits"]


def test_me_names_the_workspace_instead_of_only_its_id(client, member_key):
    """เจ้าของ key อ่าน id ฐานสิบหก 32 ตัวไม่ออก และเอาไปเทียบกับอะไรไม่ได้"""
    workspace = client.post(
        "/admin/workspaces",
        json={"code": "CS101", "name": "Intro to Programming", "term": "1/2569"},
        headers=auth(client.admin_key),
    ).json()
    user = client.post(
        "/admin/users",
        json={"external_id": "6499999999", "display_name": "Malee", "role": "member"},
        headers=auth(client.admin_key),
    ).json()
    key = client.post(
        "/admin/api-keys",
        json={"user_id": user["id"], "name": "k", "workspace_id": workspace["id"]},
        headers=auth(client.admin_key),
    ).json()

    payload = client.get("/v1/me", headers=auth(key["api_key"])).json()
    assert payload["workspace_id"] == workspace["id"]
    assert payload["workspace"]["code"] == "CS101"
    assert payload["workspace"]["name"] == "Intro to Programming"


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
def test_metrics_endpoint_exposes_prometheus(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "litegate_requests_total" in response.text


def test_registry_reload_is_admin_only(client, member_key):
    assert client.post("/admin/registry/reload", headers=auth(member_key)).status_code == 403
    response = client.post("/admin/registry/reload", headers=auth(client.admin_key))
    assert response.status_code == 200
    assert set(response.json()["models"]) == {"coding", "gemma-vision", "muse-local"}


def test_compatibility_results_drive_ready_status(client):
    headers = auth(client.admin_key)
    for feature in ("chat", "streaming"):
        client.post(
            "/admin/models/coding/compatibility",
            json={"feature": feature, "status": "pass", "test_version": "1.0"},
            headers=headers,
        )
    payload = client.get("/admin/models/coding/compatibility", headers=headers).json()
    assert payload["status"] == "READY"

    client.post(
        "/admin/models/coding/compatibility",
        json={"feature": "vision", "status": "fail", "notes": "no vision support"},
        headers=headers,
    )
    payload = client.get("/admin/models/coding/compatibility", headers=headers).json()
    assert payload["status"] == "DEGRADED"


def test_disabling_a_model_whose_file_is_gone_does_not_recreate_it(writable_config, client):
    """กด Delete แล้วกด Disable ตามติด เคยทำให้โมเดลที่ลบไปแล้วกลับมาอยู่บนดิสก์

    เกตเวย์รันหลาย worker · Disable ไปโดน worker ที่ยังถือของเก่า มันเลยเขียนไฟล์ใหม่
    จากหน่วยความจำทับที่เพิ่งลบไป
    """
    path = writable_config / "models" / "coding.yaml"
    assert path.exists()

    assert client.delete("/admin/models/coding", headers=auth(client.admin_key)).status_code == 200
    assert not path.exists()

    response = client.patch(
        "/admin/models/coding/enabled", json={"enabled": False}, headers=auth(client.admin_key)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"
    assert not path.exists(), "ไฟล์ที่ลบไปแล้วต้องไม่ถูกเขียนกลับมา"
