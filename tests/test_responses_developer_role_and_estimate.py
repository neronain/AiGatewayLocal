"""/v1/responses: บทบาท `developer` ต้องไปถึงโมเดล และ prompt ของ Codex ต้องถูกนับครบ

สองเรื่องที่ผู้ตรวจสงสัยและยืนยันแล้วว่าจริง (2026-10-06):

**developer** — ตัวแปลส่ง `role: "developer"` ต่อให้ chat completions ทั้งอย่างนั้น · Codex ใส่
คำสั่งเรื่อง sandbox กับสิทธิ์ไว้ในบทบาทนี้ทุกคำขอ · chat template ของโมเดลในบ้านรู้จักแค่
system/user/assistant/tool: บางตัวปฏิเสธบทบาทที่ไม่รู้จัก บางตัวข้ามไปเงียบ ๆ · และแค่เปลี่ยนชื่อ
เป็น system ก็ไม่พอ เพราะ `instructions` เป็น system message ตัวแรกอยู่แล้ว — system ตัวที่สอง
ถูก template ของ Qwen3 รุ่นใหม่ปฏิเสธว่า "System message must be at the beginning."

**ค่าประมาณ** — `profile_responses_request` ไม่นับนิยาม tool และชื่อ tool ที่ถูกเรียก ทั้งที่
profiler ของ chat กับ messages นับ · นิยาม tool ถูกส่งทุกเทิร์น prompt ของ Codex จึงถูกประมาณ
ต่ำกว่าที่ backend เห็นเสมอ: ด่าน context ปล่อยคำขอที่ล้นหน้าต่างไปพังที่ backend
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.multimodal import profile_openai_request, profile_responses_request
from app.core.tokens import estimate_prompt_tokens
from app.registry.schema import VisionPolicy

CHAT = "http://dgx03:8000/v1/chat/completions"
REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
CHAT_ROLES = {"system", "user", "assistant", "tool"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _text(value: str) -> list[dict]:
    return [{"type": "input_text", "text": value}]


def _sent_messages(client, key, **body) -> list[dict]:
    with respx.mock:
        route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=REPLY))
        response = client.post("/v1/responses", headers=auth(key),
                               json={"model": "coding", **body})
        assert response.status_code == 200, response.text
        return json.loads(route.calls.last.request.content)["messages"]


# ---------------------------------------------------------------------------
# developer -> system ก้อนเดียวที่ตำแหน่งแรก
# ---------------------------------------------------------------------------
def test_a_codex_shaped_request_reaches_the_backend_with_one_leading_system_message(
        client, member_key):
    messages = _sent_messages(
        client, member_key,
        instructions="You are Codex.",
        input=[
            {"type": "message", "role": "developer",
             "content": _text("<permissions>sandbox: workspace-write</permissions>")},
            {"type": "message", "role": "user", "content": _text("<environment>cwd=/repo")},
            {"type": "message", "role": "user", "content": _text("fix the failing test")},
        ])

    assert {m["role"] for m in messages} <= CHAT_ROLES, "ต้องไม่มีบทบาทที่ chat template ไม่รู้จัก"
    assert [m["role"] for m in messages] == ["system", "user", "user"]
    system = messages[0]["content"]
    # คำสั่งของ developer ต้องถึงโมเดล และมาหลัง instructions ตามลำดับที่ส่งมา
    assert system.index("You are Codex.") < system.index("sandbox: workspace-write")


def test_developer_text_reaches_the_model_even_without_instructions(client, member_key):
    messages = _sent_messages(client, member_key, input=[
        {"role": "developer", "content": "answer in Thai"},
        {"role": "user", "content": "hello"}])
    assert messages == [{"role": "system", "content": "answer in Thai"},
                        {"role": "user", "content": "hello"}]


def test_several_leading_system_and_developer_items_become_one_message(client, member_key):
    messages = _sent_messages(client, member_key, instructions="A", input=[
        {"role": "system", "content": "B"},
        {"role": "developer", "content": _text("C")},
        {"role": "user", "content": "hi"}])
    assert messages == [{"role": "system", "content": "A\n\nB\n\nC"},
                        {"role": "user", "content": "hi"}]


def test_a_developer_message_in_mid_conversation_stays_where_it_was_said(client, member_key):
    """ย้ายขึ้นหัวไม่ได้ — ลำดับคือความหมาย ("จากนี้ไปให้...") · อย่างน้อยบทบาทต้องเป็นของที่มีจริง"""
    messages = _sent_messages(client, member_key, instructions="A", input=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
        {"role": "developer", "content": "from now on, be brief"},
        {"role": "user", "content": "and now?"}])
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "system", "user"]
    assert messages[3]["content"] == "from now on, be brief"


def test_ordinary_roles_are_untouched(client, member_key):
    messages = _sent_messages(client, member_key, input=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
        {"role": "user", "content": "bye"}])
    assert messages == [{"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "hello"},
                        {"role": "user", "content": "bye"}]


# ---------------------------------------------------------------------------
# ค่าประมาณ: คำขอเดียวกันในสองรูป ต้องได้ขนาดใกล้กัน
# ---------------------------------------------------------------------------
PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "array", "items": {"type": "string"},
                    "description": "The command to execute, as argv."},
        "workdir": {"type": "string", "description": "Directory to run the command in."},
        "timeout_ms": {"type": "number", "description": "Give up after this long."},
    },
    "required": ["command"],
}
DESCRIPTION = "Runs a shell command and returns its output. " * 20
NAMES = [f"tool_number_{i}" for i in range(24)]
ARGUMENTS = json.dumps({"command": ["rg", "--files"], "workdir": "/repo"})
OUTPUT = "src/main.py\nsrc/util.py\n" * 40


def _as_responses() -> dict:
    return {
        "instructions": "You are a coding agent.",
        "tools": [{"type": "function", "name": n, "description": DESCRIPTION,
                   "parameters": PARAMETERS} for n in NAMES],
        "input": [
            {"role": "user", "content": _text("list the files")},
            {"type": "function_call", "call_id": "c1", "name": NAMES[0],
             "arguments": ARGUMENTS},
            {"type": "function_call_output", "call_id": "c1", "output": OUTPUT},
        ],
    }


def _as_chat() -> dict:
    return {
        "tools": [{"type": "function", "function": {
            "name": n, "description": DESCRIPTION, "parameters": PARAMETERS}} for n in NAMES],
        "messages": [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "list the files"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": NAMES[0], "arguments": ARGUMENTS}}]},
            {"role": "tool", "tool_call_id": "c1", "content": OUTPUT},
        ],
    }


def _estimate(profile) -> int:
    return estimate_prompt_tokens(profile)


def test_the_same_request_is_about_the_same_size_on_both_surfaces():
    """ตัวแปลจะเปลี่ยนคำขอ Responses เป็นคำขอ chat ข้างล่างนี้พอดี — backend เห็นขนาดเดียวกัน
    ด่าน context ก็ต้องเห็นขนาดเดียวกัน · เดิมฝั่ง Responses ได้ไม่ถึงหนึ่งในสิบ"""
    responses = _estimate(profile_responses_request(_as_responses(), VisionPolicy()))
    chat = _estimate(profile_openai_request(_as_chat(), VisionPolicy()))

    assert chat > 5_000, "ตัวควบคุม: คำขอนี้หนักที่นิยาม tool จริง"
    assert responses == pytest.approx(chat, rel=0.10)


def test_tool_definitions_are_counted():
    body = _as_responses()
    with_tools = _estimate(profile_responses_request(body, VisionPolicy()))
    without = _estimate(profile_responses_request(
        {k: v for k, v in body.items() if k != "tools"}, VisionPolicy()))
    assert with_tools - without > 4_000


def test_the_name_of_a_called_tool_is_counted():
    def estimate(name: str) -> int:
        return _estimate(profile_responses_request({"input": [
            {"type": "function_call", "call_id": "c1", "name": name, "arguments": "{}"},
        ]}, VisionPolicy()))

    assert estimate("a_rather_long_tool_name_" * 10) > estimate("f") + 40


def test_structured_tool_output_is_counted_as_what_it_says():
    """`output` ที่ไม่ใช่สตริงเคยถูกนับจาก repr ของ Python — นับจาก JSON ที่จะถูกส่งจริง"""
    profile = profile_responses_request({"input": [
        {"type": "function_call_output", "call_id": "c1",
         "output": {"files": ["a.py", "b.py"], "truncated": False}}]}, VisionPolicy())
    assert profile.text_chars == len('{"files":["a.py","b.py"],"truncated":false}')
