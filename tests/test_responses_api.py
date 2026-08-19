"""The Responses surface (/v1/responses) — the API Codex speaks.

Backends here speak chat completions, so almost every test is really asking the
same question: did the translation keep the meaning, in both directions, without
inventing anything the member did not send.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

UPSTREAM = "http://dgx03:8000"
CHAT = f"{UPSTREAM}/v1/chat/completions"

REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "backend-name",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "สวัสดีครับ"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def ask(client, key, **body):
    return client.post(
        "/v1/responses",
        headers=auth(key),
        json={"model": "coding", "input": "สวัสดี", **body},
    )


# ── shape ───────────────────────────────────────────────────────────────────
@respx.mock
def test_a_codex_request_comes_back_in_the_responses_shape(client, member_key):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=REPLY))

    response = ask(client, member_key)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "coding", "ต้องเป็น alias ที่ขอ ไม่ใช่ชื่อ repo ปลายทาง"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0] == {
        "type": "output_text",
        "text": "สวัสดีครับ",
        "annotations": [],
    }
    assert body["usage"]["input_tokens"] == 11
    assert body["usage"]["output_tokens"] == 4
    assert response.headers["x-litegate-protocol"] == "responses-via-openai"


@respx.mock
def test_instructions_become_a_system_message(client, member_key):
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=REPLY))

    ask(client, member_key, instructions="ตอบสั้น ๆ")

    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0] == {"role": "system", "content": "ตอบสั้น ๆ"}


@respx.mock
def test_max_output_tokens_becomes_max_tokens(client, member_key):
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=REPLY))

    ask(client, member_key, max_output_tokens=64)

    assert json.loads(route.calls[0].request.content)["max_tokens"] == 64


# ── tools ───────────────────────────────────────────────────────────────────
@respx.mock
def test_the_tool_history_survives_the_round_trip(client, member_key):
    """function_call / function_call_output อยู่ระดับเดียวกับ message ไม่ได้ซ้อนอยู่ข้างใน

    อ่านแต่ {role, content} จะทิ้งประวัติ tool ทั้งหมด — โมเดลจะเห็นว่าตัวเองขอ tool
    แล้วไม่เคยได้คำตอบ
    """
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=REPLY))

    client.post(
        "/v1/responses",
        headers=auth(member_key),
        json={
            "model": "coding",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "ไฟล์อะไรบ้าง"}]},
                {"type": "function_call", "call_id": "c1", "name": "ls", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
            ],
            "tools": [{"type": "function", "name": "ls", "parameters": {"type": "object"}}],
        },
    )

    sent = json.loads(route.calls[0].request.content)
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert sent["messages"][1]["tool_calls"][0]["function"]["name"] == "ls"
    assert sent["messages"][2] == {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}
    # tools ของ Responses แบน ส่วน chat completions ซ้อนใต้ "function"
    assert sent["tools"][0]["function"]["name"] == "ls"


@respx.mock
def test_a_tool_call_from_the_model_is_returned_as_a_function_call_item(client, member_key):
    respx.post(CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                **REPLY,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {"name": "ls", "arguments": '{"path":"."}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )
    )

    body = ask(client, member_key).json()

    item = body["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_9"
    assert item["name"] == "ls"
    assert json.loads(item["arguments"]) == {"path": "."}


# ── streaming ───────────────────────────────────────────────────────────────
def _sse(*chunks: dict) -> str:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return body + "data: [DONE]\n\n"


@respx.mock
def test_the_stream_emits_a_balanced_typed_event_sequence(client, member_key):
    """Codex อ่าน event ที่มีชนิด ไม่ใช่สายข้อความดิบ — open/close ต้องครบคู่"""
    stream = _sse(
        {"choices": [{"index": 0, "delta": {"content": "สวัส"}}]},
        {"choices": [{"index": 0, "delta": {"content": "ดี"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    )
    respx.post(CHAT).mock(
        return_value=httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
    )

    with client.stream(
        "POST", "/v1/responses", headers=auth(member_key),
        json={"model": "coding", "input": "สวัสดี", "stream": True},
    ) as response:
        assert response.status_code == 200
        raw = "".join(response.iter_text())

    events = [line[7:] for line in raw.splitlines() if line.startswith("event: ")]
    payloads = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]

    assert events[0] == "response.created"
    assert events[-1] == "response.completed"
    for required in (
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
    ):
        assert required in events, f"ขาด {required}"

    # sequence_number ต้องเดินทีละหนึ่งข้าม *ทุกชนิด* — client ใช้ตรวจว่ามี event หาย
    seqs = [p["sequence_number"] for p in payloads if "sequence_number" in p]
    assert seqs == list(range(len(seqs)))

    text = "".join(p["delta"] for p in payloads if p.get("type") == "response.output_text.delta")
    assert text == "สวัสดี"
    final = payloads[-1]["response"]
    assert final["status"] == "completed"
    assert final["output_text"] == "สวัสดี"
    assert final["model"] == "coding"
    assert final["usage"]["input_tokens"] == 5


# ── gates ───────────────────────────────────────────────────────────────────
def test_an_alias_without_the_responses_surface_is_refused(client, member_key):
    response = client.post(
        "/v1/responses",
        headers=auth(member_key),
        json={"model": "gemma-vision", "input": "hi"},
    )
    assert response.status_code == 400
    assert "responses" in response.text.lower()


def test_server_side_conversation_state_is_refused_rather_than_faked(client, member_key):
    """เกตเวย์ไม่เก็บ prompt เลย (PRD §12) — ต่อบทสนทนาที่ไม่มีหัวให้ไม่ได้"""
    response = ask(client, member_key, previous_response_id="resp_abc")
    assert response.status_code == 400
    assert "previous_response_id" in response.text


def test_an_empty_input_is_a_clean_400(client, member_key):
    response = client.post(
        "/v1/responses", headers=auth(member_key), json={"model": "coding", "input": []}
    )
    assert response.status_code == 400


@respx.mock
def test_an_image_to_a_text_only_model_is_still_refused(client, member_key):
    """ด่าน capability เดิมต้องใช้กับ surface ใหม่ด้วย — ทางเข้าที่สองต้องไม่ใช่กฎชุดที่สอง"""
    from tests.conftest import png_data_url

    response = client.post(
        "/v1/responses",
        headers=auth(member_key),
        json={
            "model": "coding",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "นี่อะไร"},
                        {"type": "input_image", "image_url": png_data_url()},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 400
    assert "image" in response.text.lower()


def test_the_catalogue_says_which_surfaces_an_alias_speaks(client, member_key):
    """เดาเอาแล้วยิงผิด surface = ได้ 400 หลังพิมพ์ prompt เสร็จ"""
    data = client.get("/v1/models", headers=auth(member_key)).json()["data"]
    coding = next(m for m in data if m["id"] == "coding")
    assert set(coding["protocols"]) == {"openai", "anthropic", "responses"}

    vision = next(m for m in data if m["id"] == "gemma-vision")
    assert "responses" not in vision["protocols"]


def test_declaring_the_surface_without_a_backend_that_can_serve_it_is_a_load_error(
    writable_config,
):
    """แปลได้เฉพาะจาก chat completions — endpoint ที่พูดแต่ Anthropic เสิร์ฟ Codex ไม่ได้"""
    import yaml

    from app.registry.store import load_snapshot

    path = writable_config / "models" / "coding.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["protocols"] = {"openai": False, "anthropic": True, "responses": True}
    for endpoint in document["spec"]["endpoints"]:
        endpoint["protocols"] = {"openai": False, "anthropic": True, "responses": False}
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))

    errors = " | ".join(load_snapshot(writable_config).errors)
    assert "protocols.responses=true requires" in errors


@pytest.fixture
def overflowing_config(writable_config):
    """เขียนก่อน `client` เสมอ — app อ่าน registry ครั้งเดียวตอน startup"""
    import yaml

    models = writable_config / "models"
    document = yaml.safe_load((models / "coding.yaml").read_text())

    wide = yaml.safe_load(yaml.safe_dump(document))
    wide["metadata"]["alias"] = "coding-long"
    wide["spec"]["endpoints"][0]["name"] = "wide"
    wide["spec"]["endpoints"][0]["base_url"] = "http://dgx-wide:8000"
    (models / "coding-long.yaml").write_text(
        yaml.safe_dump(wide, sort_keys=False, allow_unicode=True)
    )

    document["spec"]["limits"]["context_tokens"] = 2000
    document["spec"]["routing"] = {"overflow": "coding-long"}
    (models / "coding.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    )
    return writable_config


@respx.mock
def test_routing_rules_apply_on_this_surface_too(overflowing_config, client, member_key):
    """ทางเข้าที่สามต้องได้กฎชุดเดียวกับอีกสองทาง ไม่ใช่กฎของตัวเอง"""
    near = respx.post(CHAT).mock(return_value=httpx.Response(200, json=REPLY))
    far = respx.post("http://dgx-wide:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    response = client.post(
        "/v1/responses",
        headers=auth(member_key),
        json={"model": "coding", "input": "ก" * 200_000},
    )

    assert response.status_code == 200, response.text
    assert far.called and not near.called
    assert response.json()["model"] == "coding"
