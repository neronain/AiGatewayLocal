"""ตัวแปลขาเข้าต้องไม่ทิ้งเนื้อหาของคำขอเงียบ ๆ — ส่งต่อ หรือปฏิเสธพร้อมบอกว่าส่วนไหน

ตรวจ 2026-10-06 บนโมเดลที่เสิร์ฟผ่านตัวแปล (เครื่องพูดแต่ chat completions — คือโมเดลในบ้าน
เกือบทุกตัว):

    /v1/messages   tool_result ที่มีรูป + บล็อก document
                   → backend ได้ {"role":"tool","tool_call_id":"toolu_1","content":""}
                     ไบต์ของรูปกับข้อความในเอกสารไม่เคยไปถึง · แต่ยังถูกบังคับ vision และ
                     บันทึก visual_input_tokens

    /v1/responses  text.format = json_schema + tool ชนิด custom
                   → backend ได้ ['max_tokens','messages','model','tools']
                     ไม่มี response_format · tool ชนิด custom หายไป

โมเดลตอบโดยไม่เห็นสิ่งที่ผู้ใช้ส่งมา และตอบ 200 · เทสในไฟล์นี้ดู **ไบต์ที่ backend ได้รับจริง**
(หาเครื่องหมายที่ฝังไว้ในเนื้อหา) หรือดูว่าได้ 400 ที่ระบุตำแหน่งและ backend ไม่ถูกเรียก —
ไม่มีทางที่สาม
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

from tests.conftest import png_data_url

CODING = "http://dgx03:8000"   # coding · text-only · เครื่องพูดแต่ openai
GEMMA = "http://dgx02:8000"    # gemma-vision · รับรูป · เครื่องพูดแต่ openai
MUSE = "http://dgx01:8000"     # muse-local · llama.cpp ที่พูด Anthropic เอง

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
STREAM = (b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
          b"data: [DONE]\n\n")

PNG = png_data_url(64, 48)
B64 = PNG.split(",", 1)[1]
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": B64}}
PDF = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                      "data": "JVBERi0xLjQK"}}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def vision_everywhere(writable_config):
    """gemma-vision เปิดทั้ง /v1/messages และ /v1/responses — เสิร์ฟผ่านตัวแปลทั้งคู่"""
    path = writable_config / "models" / "gemma-vision.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["protocols"].update({"anthropic": True, "responses": True})
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    return writable_config


@pytest.fixture
def backend():
    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=STREAM,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=REPLY)

    with respx.mock:
        yield {
            "gemma": respx.post(f"{GEMMA}/v1/chat/completions").mock(side_effect=answer),
            "coding": respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=answer),
            "coding-responses": respx.post(f"{CODING}/v1/responses").mock(
                return_value=httpx.Response(200, json={
                    "id": "resp_1", "object": "response", "status": "completed",
                    "model": "up", "output": [],
                    "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}})),
            "muse-native": respx.post(f"{MUSE}/v1/messages").mock(
                return_value=httpx.Response(200, json=NATIVE_REPLY)),
        }


def _raw(route) -> str:
    return route.calls.last.request.content.decode()


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


def _messages(client, key, model="gemma-vision", **body):
    return client.post("/v1/messages", headers=auth(key),
                       json={"model": model, "max_tokens": 64, **body})


def _responses(client, key, model="coding", **body):
    return client.post("/v1/responses", headers=auth(key), json={"model": model, **body})


def _refused(client, routes, response, where: str, *, anthropic: bool) -> dict:
    """400 ที่ระบุตำแหน่ง · backend ไม่ถูกเรียก · ไม่มีแถว usage (= ไม่มีการคิดเงิน)"""
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    error = body["error"]
    if anthropic:
        assert body["type"] == "error" and error["type"] == "invalid_request_error"
    else:
        assert error["type"] == "invalid_request_error" and error["param"] == where
    assert error["message"].startswith(f"{where}:"), error["message"]
    assert not any(route.called for route in routes.values()), "ต้องไม่มีอะไรไปถึง backend"
    assert _usage_rows(client) == 0
    return error


# ===========================================================================
# /v1/messages
# ===========================================================================
TOOL_TURN = [
    {"role": "user", "content": "what is in screenshot.png and in spec.txt?"},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Read",
         "input": {"path": "screenshot.png"}}]},
]
READ = [{"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}]


def test_an_image_returned_by_a_tool_and_a_text_document_reach_the_backend(
        vision_everywhere, backend, client, member_key):
    """เคสที่ตรวจพบ ตรงตัว"""
    response = _messages(client, member_key, tools=READ, messages=[*TOOL_TURN, {
        "role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [IMAGE]},
            {"type": "document", "title": "spec.txt",
             "source": {"type": "text", "media_type": "text/plain",
                        "data": "SPEC: the launch code is 4471"}},
            {"type": "text", "text": "go on"}]}])

    assert response.status_code == 200, response.text
    sent = _sent(backend["gemma"])["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "user"]

    # ผลของ tool ยังเป็น tool message ที่ตามหลังการเรียกทันที และบอกว่ามีรูปตามมา
    tool = sent[2]
    assert tool["tool_call_id"] == "toolu_1"
    assert "image" in tool["content"] and tool["content"] != ""

    # รูป — ไบต์เดิมทุกไบต์ — อยู่ใน user message ถัดไป พร้อมข้อความของเอกสารและของผู้ใช้
    follow_up = sent[3]["content"]
    images = [p for p in follow_up if p["type"] == "image_url"]
    assert [p["image_url"]["url"] for p in images] == [PNG]
    text = "\n".join(p["text"] for p in follow_up if p["type"] == "text")
    assert "toolu_1" in text, "ต้องโยงรูปกลับไปหาการเรียก tool ที่คืนมันมา"
    assert "SPEC: the launch code is 4471" in text
    assert "spec.txt" in text
    assert text.rstrip().endswith("go on")


def test_several_tool_results_stay_together_and_their_images_follow(
        vision_everywhere, backend, client, member_key):
    """chat completions ให้ tool message ของการเรียกชุดเดียวกันอยู่ติดกัน — รูปต้องไม่แทรกกลาง"""
    response = _messages(client, member_key, tools=READ, messages=[
        {"role": "user", "content": "compare a.png with b.txt"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a.png"}},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"path": "b.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "a.png, 64x48"}, IMAGE]},
            {"type": "tool_result", "tool_use_id": "t2", "content": "CONTENTS-OF-B"}]}])

    assert response.status_code == 200, response.text
    sent = _sent(backend["gemma"])["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "tool", "user"]
    assert sent[2]["content"].startswith("a.png, 64x48")
    assert sent[3] == {"role": "tool", "tool_call_id": "t2", "content": "CONTENTS-OF-B"}
    assert sent[4]["content"][-1]["image_url"]["url"] == PNG


@pytest.mark.parametrize("block, marker", [
    ({"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                     "data": "MARK-plain-text-doc"}}, "MARK-plain-text-doc"),
    ({"type": "document", "source": {"type": "content", "content": "MARK-content-string"}},
     "MARK-content-string"),
    ({"type": "document", "context": "MARK-context", "source": {
        "type": "content", "content": [{"type": "text", "text": "MARK-content-block"}]}},
     "MARK-content-block"),
    ({"type": "document", "context": "MARK-context", "source": {
        "type": "text", "media_type": "text/plain", "data": "x"}}, "MARK-context"),
    ({"type": "tool_result", "tool_use_id": "toolu_1", "content": [
        {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                        "data": "MARK-doc-in-tool-result"}}]},
     "MARK-doc-in-tool-result"),
    ({"type": "tool_result", "tool_use_id": "toolu_1", "content": "MARK-string-result"},
     "MARK-string-result"),
    ({"type": "text", "text": "MARK-text", "cache_control": {"type": "ephemeral"}}, "MARK-text"),
], ids=["text-document", "content-string", "content-blocks", "document-context",
        "document-in-tool-result", "string-tool-result", "text-with-cache-control"])
def test_content_that_chat_completions_can_carry_is_carried(
        vision_everywhere, backend, client, member_key, block, marker):
    response = _messages(client, member_key, tools=READ,
                         messages=[*TOOL_TURN, {"role": "user", "content": [block]}])
    assert response.status_code == 200, response.text
    assert marker in _raw(backend["gemma"])


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
@pytest.mark.parametrize("content, where", [
    ([{"type": "text", "text": "summarise"}, PDF], "messages[0].content[1]"),
    ([{"type": "document", "source": {"type": "url", "url": "https://x.test/a.pdf"}}],
     "messages[0].content[0]"),
    ([{"type": "document", "source": {"type": "file", "file_id": "file_1"}}],
     "messages[0].content[0]"),
    ([{"type": "tool_result", "tool_use_id": "t", "content": [PDF]}],
     "messages[0].content[0].content"),
    ([{"type": "tool_use", "id": "t", "name": "Read", "input": {}}], "messages[0].content[0]"),
], ids=["pdf", "pdf-url", "file-id", "pdf-in-tool-result", "tool_use-from-user"])
def test_content_that_cannot_be_carried_is_refused_not_dropped(
        vision_everywhere, backend, client, member_key, content, where, stream):
    response = _messages(client, member_key, stream=stream,
                         messages=[{"role": "user", "content": content}])
    # สตรีมก็ต้องได้ 400 เป็น JSON ทั้งก้อน — ไม่ใช่ 200 text/event-stream ที่พังตอนแปล
    _refused(client, backend, response, where, anthropic=True)


@pytest.mark.parametrize("tool", [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
], ids=lambda t: t["type"])
def test_a_tool_only_anthropics_api_can_run_is_refused(
        vision_everywhere, backend, client, member_key, tool):
    """เดิมถูกส่งเป็น function ที่ไม่มี schema — โมเดล "เรียก" ได้ แต่ไม่มีใครรัน"""
    response = _messages(client, member_key, tools=[*READ, tool],
                         messages=[{"role": "user", "content": "search the web"}])
    error = _refused(client, backend, response, "tools[1]", anthropic=True)
    assert tool["type"] in error["message"]


def test_custom_tools_with_or_without_the_type_field_are_forwarded(
        vision_everywhere, backend, client, member_key):
    response = _messages(client, member_key, tools=[
        READ[0], {"type": "custom", "name": "Write", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "hi"}])
    assert response.status_code == 200, response.text
    assert [t["function"]["name"] for t in _sent(backend["gemma"])["tools"]] == ["Read", "Write"]


def test_request_fields_chat_completions_can_express_are_translated(
        vision_everywhere, backend, client, member_key):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    response = _messages(
        client, member_key, tools=READ, top_k=40, temperature=0.2, top_p=0.9,
        stop_sequences=["END"],
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        output_config={"format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "hi"}])

    assert response.status_code == 200, response.text
    sent = _sent(backend["gemma"])
    assert sent["top_k"] == 40 and sent["temperature"] == 0.2 and sent["top_p"] == 0.9
    assert sent["stop"] == ["END"]
    assert sent["tool_choice"] == "auto" and sent["parallel_tool_calls"] is False
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] == schema
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}


def test_hints_that_do_not_change_the_question_are_ignored_not_refused(
        vision_everywhere, backend, client, member_key):
    """Claude Code ส่งพวกนี้เป็นปกติ — ไม่มีผลกับสิ่งที่โมเดลถูกถาม จึงไม่ใช่เหตุให้ปฏิเสธ"""
    response = _messages(
        client, member_key,
        metadata={"user_id": "u1"}, service_tier="auto",
        thinking={"type": "enabled", "budget_tokens": 2000},
        context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "PRIVATE-THOUGHT", "signature": "s"},
                {"type": "text", "text": "hello"}]},
            {"role": "user", "content": "bye"}])

    assert response.status_code == 200, response.text
    raw = _raw(backend["gemma"])
    assert "PRIVATE-THOUGHT" not in raw, "ความคิดของโมเดลอื่นไม่ถูกส่งต่อ (ตั้งใจ)"
    assert "context_management" not in raw and "metadata" not in raw


def test_a_backend_that_speaks_anthropic_itself_still_gets_the_request_whole(
        backend, client, member_key):
    """muse-local คือ llama.cpp ที่พูด /v1/messages เอง — ไม่มีการแปล จึงไม่มีอะไรให้ปฏิเสธ"""
    response = _messages(client, member_key, model="muse-local", messages=[
        {"role": "user", "content": [{"type": "text", "text": "summarise"}, PDF]}])

    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-protocol"] == "anthropic-native"
    assert _sent(backend["muse-native"])["messages"][0]["content"][1] == PDF


def test_what_is_billed_is_what_was_forwarded(vision_everywhere, client, member_key):
    """ข้อความในเอกสารถูกส่งแล้ว ก็ต้องถูกนับ — ทั้งที่ด่าน context และใน count_tokens"""
    text = "lorem ipsum dolor sit amet " * 2000         # 54,000 อักขระ ≈ 13,500 token

    def count(content) -> int:
        counted = client.post("/v1/messages/count_tokens", headers=auth(member_key), json={
            "model": "gemma-vision", "messages": [{"role": "user", "content": content}]})
        assert counted.status_code == 200, counted.text
        return counted.json()["input_tokens"]

    bare = count([{"type": "text", "text": "summarise"}])
    with_document = count([
        {"type": "text", "text": "summarise"},
        {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                        "data": text}}])
    in_tool_result = count([{"type": "tool_result", "tool_use_id": "t", "content": [
        {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                        "data": text}}]}])

    assert with_document - bare == pytest.approx(len(text) / 4, rel=0.02)
    assert in_tool_result == pytest.approx(len(text) / 4, rel=0.02)


# ===========================================================================
# /v1/responses
# ===========================================================================
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
          "additionalProperties": False}
SHELL = {"type": "function", "name": "shell", "parameters": {"type": "object"}}


def test_structured_output_reaches_the_backend_as_response_format(
        backend, client, member_key):
    """เคสที่ตรวจพบ ครึ่งแรก"""
    response = _responses(
        client, member_key, input="Where is the Eiffel tower? Answer as JSON.",
        text={"format": {"type": "json_schema", "name": "place", "strict": True,
                         "schema": SCHEMA}})

    assert response.status_code == 200, response.text
    assert _sent(backend["coding"])["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "place", "schema": SCHEMA, "strict": True}}


@pytest.mark.parametrize("text, expected", [
    ({"format": {"type": "json_object"}}, {"type": "json_object"}),
    ({"format": {"type": "text"}}, None),
    ({"verbosity": "low"}, None),
], ids=["json_object", "plain-text", "verbosity-only"])
def test_other_text_settings(backend, client, member_key, text, expected):
    response = _responses(client, member_key, input="hi", text=text)
    assert response.status_code == 200, response.text
    assert _sent(backend["coding"]).get("response_format") == expected


def test_a_custom_tool_is_refused_not_dropped(backend, client, member_key):
    """เคสที่ตรวจพบ ครึ่งหลัง — เดิม apply_patch หายจากรายการ โมเดลไม่รู้ว่ามี"""
    response = _responses(client, member_key, input="patch it", tools=[
        {"type": "custom", "name": "apply_patch", "description": "freeform patch"}, SHELL])
    error = _refused(client, backend, response, "tools[0]", anthropic=False)
    assert "function tool" in error["message"]


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
def test_a_hosted_tool_definition_is_skipped_and_the_caller_is_told(
        backend, client, member_key, stream):
    """เครื่องมือที่ OpenAI รันเอง: client บางตัวแนบมาทุกคำขอ — ข้าม แต่ไม่เงียบ"""
    with client.stream("POST", "/v1/responses", headers=auth(member_key), json={
            "model": "coding", "input": "hi", "stream": stream,
            "tools": [SHELL, {"type": "web_search", "external_web_access": False},
                      {"type": "image_generation"}]}) as response:
        response.read()

    assert response.status_code == 200
    assert [t["function"]["name"] for t in _sent(backend["coding"])["tools"]] == ["shell"]
    assert response.headers["x-litegate-ignored"] == (
        "tools[1]:web_search, tools[2]:image_generation")


def test_a_request_with_nothing_skipped_carries_no_notice(backend, client, member_key):
    response = _responses(client, member_key, input="hi", tools=[SHELL])
    assert response.status_code == 200, response.text
    assert "x-litegate-ignored" not in response.headers


@pytest.mark.parametrize("extra, where", [
    ({"tools": [SHELL, {"type": "web_search"}], "tool_choice": {"type": "web_search"}},
     "tool_choice"),
    ({"tools": [SHELL], "tool_choice": {"type": "allowed_tools", "mode": "auto",
                                        "tools": [{"type": "function", "name": "shell"}]}},
     "tool_choice"),
    ({"conversation": "conv_123"}, "conversation"),
    ({"prompt": {"id": "pmpt_1"}}, "prompt"),
    ({"background": True}, "background"),
], ids=["forced-hosted-tool", "allowed_tools", "conversation", "prompt-template",
        "background"])
def test_a_request_that_cannot_be_honoured_is_refused(
        backend, client, member_key, extra, where):
    response = _responses(client, member_key, input="hi", **extra)
    _refused(client, backend, response, where, anthropic=False)


def test_naming_a_function_tool_still_works(backend, client, member_key):
    response = _responses(client, member_key, input="hi", tools=[SHELL],
                          tool_choice={"type": "function", "name": "shell"})
    assert response.status_code == 200, response.text
    assert _sent(backend["coding"])["tool_choice"] == {
        "type": "function", "function": {"name": "shell"}}


USER = {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}


@pytest.mark.parametrize("item, where", [
    ({"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": "*** x"},
     "input[1]"),
    ({"type": "custom_tool_call_output", "call_id": "c1", "output": "done"}, "input[1]"),
    ({"type": "web_search_call", "id": "ws_1", "status": "completed"}, "input[1]"),
    ({"type": "local_shell_call", "call_id": "c1", "action": {"type": "exec"}}, "input[1]"),
    ({"type": "item_reference", "id": "msg_123"}, "input[1]"),
    ({"type": "something_new", "payload": 1}, "input[1]"),
    ({"role": "user", "content": [{"type": "input_text", "text": "x"},
                                 {"type": "computer_screenshot", "image_url": PNG}]},
     "input[1].content[1]"),
    ({"type": "function_call_output", "call_id": "c1",
      "output": [{"type": "input_file", "file_id": "file_1"}]}, "input[1].output"),
], ids=["custom_tool_call", "custom_tool_call_output", "web_search_call", "local_shell_call",
        "item_reference", "unknown-item", "unknown-content-part", "file-in-tool-output"])
def test_an_input_item_that_cannot_be_carried_is_refused_not_dropped(
        backend, client, member_key, item, where):
    response = _responses(client, member_key, input=[USER, item])
    _refused(client, backend, response, where, anthropic=False)


def test_items_the_translator_does_carry(backend, client, member_key):
    response = _responses(client, member_key, tools=[SHELL], input=[
        USER,
        {"type": "reasoning", "id": "rs_1", "summary": [],
         "content": [{"type": "reasoning_text", "text": "PRIVATE-THOUGHT"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "refusal", "refusal": "MARK-refusal"}]},
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "input_text", "text": "MARK-part-output"}]},
        {"type": "function_call", "call_id": "c2", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": {"exit": 0, "out": "ไทย"}},
    ])

    assert response.status_code == 200, response.text
    sent = _sent(backend["coding"])["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "assistant", "tool",
                                         "assistant", "tool"]
    assert sent[1]["content"] == "MARK-refusal"
    assert sent[3]["content"] == "MARK-part-output"
    assert json.loads(sent[5]["content"]) == {"exit": 0, "out": "ไทย"}
    assert "PRIVATE-THOUGHT" not in _raw(backend["coding"]), "reasoning ของรอบก่อนไม่ถูกส่งต่อ (ตั้งใจ)"


def test_an_image_returned_by_a_tool_reaches_a_vision_model_as_an_image(
        vision_everywhere, backend, client, member_key):
    """เดิมรายการชิ้นส่วนถูก json.dumps ทั้งก้อน — โมเดลได้ base64 ของรูปเป็น *ข้อความ*"""
    response = _responses(client, member_key, model="gemma-vision", tools=[SHELL], input=[
        USER,
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
        {"type": "function_call", "call_id": "c2", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "screenshot.png, 64x48"},
            {"type": "input_image", "image_url": PNG}]},
        {"type": "function_call_output", "call_id": "c2", "output": "plain text"},
        {"role": "user", "content": [{"type": "input_text", "text": "what do you see?"}]},
    ])

    assert response.status_code == 200, response.text
    sent = _sent(backend["gemma"])["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "tool", "user", "user"]
    assert sent[2]["content"].startswith("screenshot.png, 64x48")
    assert B64 not in sent[2]["content"], "ไบต์ของรูปต้องไม่ไปเป็นข้อความ"
    carried = sent[4]["content"]
    assert "c1" in carried[0]["text"]
    assert carried[1] == {"type": "image_url", "image_url": {"url": PNG}}
    # และถูกนับเป็นรูปหนึ่งใบ ไม่ใช่ข้อความหลายร้อย token
    assert response.json()["usage"]["litegate"]["visual_input_tokens"] > 0


def test_tool_images_obey_the_same_gates_as_images_the_user_attached(
        vision_everywhere, backend, client, member_key):
    def ask(model: str, images: int):
        return _responses(client, member_key, model=model, tools=[SHELL], input=[
            USER, {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_image", "image_url": PNG}] * images}])

    text_only = ask("coding", 1)
    assert text_only.status_code == 400
    assert text_only.json()["error"]["code"] == "MODEL_CAPABILITY_NOT_SUPPORTED"

    too_many = ask("gemma-vision", 9)
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "TOO_MANY_IMAGES"
    assert not any(route.called for route in backend.values())


def test_a_backend_that_speaks_responses_itself_still_gets_the_request_whole(
        writable_config, backend, client, member_key):
    path = writable_config / "models" / "coding.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["endpoints"][0]["protocols"]["responses"] = True
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    client.app.state.services.registry.reload()

    custom = {"type": "custom", "name": "apply_patch", "description": "freeform patch"}
    response = _responses(client, member_key, input=[
        USER, {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch",
               "input": "*** Begin Patch"}],
        tools=[custom, {"type": "web_search"}])

    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-protocol"] == "responses-native"
    sent = _sent(backend["coding-responses"])
    assert sent["tools"] == [custom, {"type": "web_search"}]
    assert sent["input"][1]["type"] == "custom_tool_call"
    assert "x-litegate-ignored" not in response.headers, "ไม่ได้แปล จึงไม่มีอะไรถูกข้าม"


def test_the_schema_of_a_structured_output_is_counted(client):
    from app.core.multimodal import profile_responses_request
    from app.registry.schema import VisionPolicy

    big = {"type": "object", "properties": {
        f"field_{i}": {"type": "string", "description": "x" * 80} for i in range(100)}}
    plain = profile_responses_request({"input": "hi"}, VisionPolicy())
    shaped = profile_responses_request(
        {"input": "hi", "text": {"format": {"type": "json_schema", "name": "n",
                                            "schema": big}}}, VisionPolicy())
    assert shaped.text_chars - plain.text_chars >= len(json.dumps(big, separators=(",", ":")))


# ===========================================================================
# ตัวตรวจกับตัวแปลต้องตัดสินตรงกันเสมอ
# ===========================================================================
def test_whatever_the_inspector_passes_the_translator_translates():
    """`untranslatable` ถูกถามก่อนเปิดสตรีม · ตัวแปลถูกเรียกหลังเปิดแล้ว — ถ้าตัวแรกปล่อยแต่
    ตัวหลังโยน client จะได้สตรีมขาด · สองตัวจึงต้องเห็นคำขอเดียวกันเป็นคำตัดสินเดียวกัน"""
    from app.core.errors import GatewayError
    from app.upstream.protocol import anthropic, responses

    anthropic_bodies = [
        {"messages": [{"role": "user", "content": [PDF]}]},
        {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [IMAGE]}]}]},
        {"messages": [{"role": "user", "content": "hi"}],
         "tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {"messages": [{"role": "user", "content": "hi"}], "mcp_servers": [{"name": "x"}]},
        {"messages": [{"role": "user", "content": "hi"}], "tool_choice": {"type": "weird"}},
        {"messages": [{"role": "assistant", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "x"}]}]},
    ]
    responses_bodies = [
        {"input": "hi", "tools": [{"type": "custom", "name": "p"}]},
        {"input": "hi", "tools": [{"type": "web_search"}]},
        {"input": [{"type": "item_reference", "id": "x"}]},
        {"input": [{"type": "function_call_output", "call_id": "c",
                    "output": [{"type": "input_image", "image_url": PNG}]}]},
        {"input": [{"role": "user", "content": [{"type": "mystery"}]}]},
    ]
    for module, translate, bodies in (
        (anthropic, anthropic.anthropic_to_openai_request, anthropic_bodies),
        (responses, responses.responses_to_openai_request, responses_bodies),
    ):
        for body in bodies:
            problems = module.untranslatable(body)
            try:
                translate(body, "up")
                translated = True
            except GatewayError:
                translated = False
            assert translated == (not problems), body
