"""structured output ที่ตัวแปลแปลไม่ได้ ต้องถูกปฏิเสธพร้อมบอกตำแหน่ง — ไม่ใช่หายเงียบ ๆ

ตัวแปลของ /v1/responses (`text.format`) และ /v1/messages (`output_config.format`) สร้าง
`response_format` ของ chat เฉพาะเมื่อรูปตรงเป๊ะ · รูปอื่นทั้งหมดคืน None แล้วคำขอถูกส่งต่อ
**โดยไม่มี response_format เลย** ตรวจ 2026-10-09 กับ `coding` (เสิร์ฟผ่านตัวแปล):

    /v1/responses  text.format = {"type":"json_schema","name":"place"}              (ลืม schema)
    /v1/responses  text.format = {"type":"json_schema","name":…,"parameters":{…}}   (ชื่อของ tool)
    /v1/responses  text.format = {"type":"json_schema","json_schema":{…}}           (รูปของ chat)
    /v1/messages   output_config.format = {"type":"json_schema"}
        → 200 · backend ได้ ['max_tokens','messages','model'] · โมเดลตอบข้อความอิสระ

อาการเดียวกับที่วัดจาก llama.cpp บนทาง chat (ดู tests/test_response_format_arrives_valid.py)
ต่างกันที่คนทิ้งคือเกตเวย์เอง · กติกาของตัวแปลมีอยู่แล้ว (tests/test_translation_drops_nothing.py):
ส่งต่อ หรือ 400 ที่ระบุตำแหน่งและ backend ไม่ถูกเรียก — ไม่มีทางที่สาม

ทางนี้ **ปฏิเสธอย่างเดียว ไม่ซ่อม**: SDK ของสอง surface นี้ส่งรูปที่ถูกอยู่แล้ว (รูปเพี้ยนที่พบในของ
จริงมาจาก LangChain / OpenAI SDK บนทาง chat) และการซ่อมต้องมีช่องบอกผู้เรียกเพิ่มอีกทาง
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

CODING = "http://dgx03:8000"   # coding · เครื่องพูดแต่ openai — สอง surface นี้จึงผ่านตัวแปล
MUSE = "http://dgx01:8000"     # muse-local · llama.cpp ที่พูด Anthropic เอง

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
          "additionalProperties": False}
REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 50, "completion_tokens": 2, "total_tokens": 52},
}
NATIVE_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "up",
    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 50, "output_tokens": 2},
}
HI = [{"role": "user", "content": "Where is the Eiffel tower? Answer as JSON."}]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def backend():
    with respx.mock:
        yield {
            "coding": respx.post(f"{CODING}/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=REPLY)),
            "coding-responses": respx.post(f"{CODING}/v1/responses").mock(
                return_value=httpx.Response(200, json={
                    "id": "resp_1", "object": "response", "status": "completed",
                    "model": "up", "output": [],
                    "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}})),
            "muse-native": respx.post(f"{MUSE}/v1/messages").mock(
                return_value=httpx.Response(200, json=NATIVE_REPLY)),
        }


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def _usage_rows(client) -> int:
    from sqlalchemy import func, select

    from app.db.models import UsageLog
    from app.db.session import session_scope

    client.portal.call(client.app.state.services.usage.flush)

    async def count() -> int:
        async with session_scope() as session:
            return (await session.execute(select(func.count()).select_from(UsageLog))).scalar()

    return client.portal.call(count)


def _responses(client, key, wanted, model="coding"):
    return client.post("/v1/responses", headers=auth(key), json={
        "model": model, "input": HI[0]["content"], "text": {"format": wanted}})


def _messages(client, key, model="coding", **body):
    return client.post("/v1/messages", headers=auth(key), json={
        "model": model, "max_tokens": 64, "messages": HI, **body})


def _refused(client, routes, response, where: str, *, anthropic: bool) -> str:
    """400 ที่ระบุตำแหน่ง · backend ไม่ถูกเรียก · ไม่มีแถว usage (= ไม่มีการคิดเงิน)"""
    assert response.status_code == 400, response.text
    body = response.json()
    error = body["error"]
    assert error["type"] == "invalid_request_error"
    if anthropic:
        assert body["type"] == "error"
    else:
        assert error["param"] == where
    assert error["message"].startswith(f"{where}:"), error["message"]
    assert not any(route.called for route in routes.values()), "ต้องไม่มีอะไรไปถึง backend"
    assert _usage_rows(client) == 0
    return error["message"]


# ===========================================================================
# /v1/responses — text.format
# ===========================================================================
RESPONSES_DROPPED = {
    "no-schema": ({"type": "json_schema", "name": "place", "strict": True},
                  "text.format.schema"),
    "schema-is-a-string": ({"type": "json_schema", "name": "place", "schema": "object"},
                           "text.format.schema"),
    # ชื่อฟิลด์ของ function tool ของ surface เดียวกันนี้เอง
    "parameters-for-schema": ({"type": "json_schema", "name": "place", "parameters": SCHEMA},
                              "text.format.schema"),
    # รูปของ chat completions ถูกส่งมาทาง Responses
    "chat-shaped": ({"type": "json_schema", "json_schema": {"name": "place", "schema": SCHEMA}},
                    "text.format.schema"),
    "a-type-with-no-equivalent": ({"type": "grammar", "syntax": "lark", "definition": "x"},
                                  "text.format.type"),
    "no-type": ({"name": "place", "schema": SCHEMA}, "text.format.type"),
}


@pytest.mark.parametrize("case", RESPONSES_DROPPED)
def test_a_format_the_translator_cannot_carry_is_refused_on_responses(
        backend, client, member_key, case):
    wanted, where = RESPONSES_DROPPED[case]
    message = _refused(client, backend, _responses(client, member_key, wanted), where,
                       anthropic=False)
    if where.endswith(".schema"):
        # บอกรูปที่ถูกของ surface นี้ (แบน) ไม่ใช่ของ chat
        assert '{"type": "json_schema", "name": "...", "schema": {...}}' in message


@pytest.mark.parametrize("wanted, expected", [
    ({"type": "json_schema", "name": "place", "strict": True, "schema": SCHEMA},
     {"type": "json_schema", "json_schema": {"name": "place", "schema": SCHEMA, "strict": True}}),
    ({"type": "json_object"}, {"type": "json_object"}),
    ({"type": "text"}, None),
    ({}, None),
], ids=["json_schema", "json_object", "text", "empty"])
def test_formats_the_translator_carries_still_pass(backend, client, member_key, wanted, expected):
    """ตัวควบคุม — ด่านใหม่ต้องไม่ปฏิเสธของที่เคยใช้ได้"""
    response = _responses(client, member_key, wanted)
    assert response.status_code == 200, response.text
    assert _sent(backend["coding"]).get("response_format") == expected


def test_a_backend_that_speaks_responses_itself_judges_the_format_itself(
        writable_config, backend, client, member_key):
    """ไม่ได้แปล = ไม่มีอะไรหาย · รูปที่เราแปลไม่เป็นอาจเป็นของที่เครื่องนั้นรู้จัก"""
    path = writable_config / "models" / "coding.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["endpoints"][0]["protocols"]["responses"] = True
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    client.app.state.services.registry.reload()

    wanted = {"type": "grammar", "syntax": "lark", "definition": "x"}
    response = _responses(client, member_key, wanted)

    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-protocol"] == "responses-native"
    assert _sent(backend["coding-responses"])["text"] == {"format": wanted}


# ===========================================================================
# /v1/messages — output_config.format (และ output_format ชื่อช่วง beta)
# ===========================================================================
MESSAGES_DROPPED = {
    "no-schema": ({"output_config": {"format": {"type": "json_schema"}}},
                  "output_config.format.schema"),
    "chat-shaped": (
        {"output_config": {"format": {"type": "json_schema",
                                      "json_schema": {"name": "place", "schema": SCHEMA}}}},
        "output_config.format.schema"),
    # Anthropic มีแต่ json_schema · json_object เป็นของ OpenAI
    "json_object": ({"output_config": {"format": {"type": "json_object"}}},
                    "output_config.format.type"),
    "beta-name-no-schema": ({"output_format": {"type": "json_schema", "name": "place"}},
                            "output_format.schema"),
}


@pytest.mark.parametrize("case", MESSAGES_DROPPED)
def test_a_format_the_translator_cannot_carry_is_refused_on_messages(
        backend, client, member_key, case):
    body, where = MESSAGES_DROPPED[case]
    _refused(client, backend, _messages(client, member_key, **body), where, anthropic=True)


@pytest.mark.parametrize("body", [
    {"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}},
    {"output_format": {"type": "json_schema", "schema": SCHEMA}},
], ids=["output_config", "beta-name"])
def test_a_schema_still_reaches_the_backend_from_messages(backend, client, member_key, body):
    """ตัวควบคุม"""
    response = _messages(client, member_key, **body)
    assert response.status_code == 200, response.text
    assert _sent(backend["coding"])["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": SCHEMA, "strict": True}}


def test_effort_without_a_format_is_not_a_format_problem(backend, client, member_key):
    """ตัวควบคุม — `output_config` ไม่ได้มีแต่ format"""
    response = _messages(client, member_key, output_config={"effort": "low"})
    assert response.status_code == 200, response.text
    assert "response_format" not in _sent(backend["coding"])


def test_a_backend_that_speaks_anthropic_itself_judges_the_format_itself(
        backend, client, member_key):
    body = {"output_config": {"format": {"type": "json_schema"}}}
    response = _messages(client, member_key, model="muse-local", **body)

    assert response.status_code == 200, response.text
    assert _sent(backend["muse-native"])["output_config"] == body["output_config"]
