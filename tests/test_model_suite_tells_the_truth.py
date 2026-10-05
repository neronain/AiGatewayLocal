"""ป้ายเขียวของชุดทดสอบโมเดลต้องแปลว่าโมเดลตอบจริง — ไม่ใช่แค่ว่า HTTP ตอบ 200

ปุ่ม Test ในคอนโซลคือที่เดียวที่แอดมินใช้ตัดสินว่าโมเดลพร้อมให้คนใช้ · ตรวจ 2026-10-05:

* **MODEL-001** รายงาน pass พร้อมข้อความ `replied ''` — โมเดล reasoning ที่ใช้งบ 16 token
  หมดไปกับการคิดไม่เคยตอบอะไรเลย แต่ได้ป้ายเขียว
* **MODEL-003** เติม prompt แค่ 25% ของหน้าต่างที่ประกาศ — ทะเบียนที่ประกาศเกินจริง 2 เท่า
  (เอา `--ctx-size` ก้อนรวมของ llama.cpp มาใส่) ผ่าน "long_context" ทั้งที่คำขอจริงจะถูกปฏิเสธ
* **MODEL-009** ผ่านด้วย `content: [{"type": "text", "text": ""}]` ที่ตัวแปลเติมให้เอง

ชุดทดสอบรันกับเกตเวย์จริงในเทสนี้ (ผ่าน ASGI) โดยมี backend ปลอมที่ทำตัวเหมือนของจริงใน
จุดที่สำคัญ: นับ token ของ prompt และปฏิเสธเมื่อเกินหน้าต่าง *ของมันเอง*
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.modeltest import ModelTestSuite

CODING = "http://dgx03:8000"   # coding · ประกาศ 262,144 · reasoning: false
MUSE = "http://dgx01:8000"     # muse-local · reasoning: true · max_output 8,192
DECLARED = 262_144


def run(client, alias: str, *tests: str, transport=httpx.ASGITransport) -> dict:
    """รันชุดทดสอบจริงกับแอปตัวนี้ บน event loop ของมันเอง · คืน {test_id: TestResult}"""
    async def go():
        suite = ModelTestSuite("http://gw", client.admin_key, alias, timeout=30)
        headers = dict(suite._client.headers)
        await suite._client.aclose()
        suite._client = httpx.AsyncClient(
            transport=transport(app=client.app), base_url="http://gw",
            headers=headers, timeout=30)
        try:
            return await suite.run(only=set(tests))
        finally:
            await suite.aclose()

    return {result.test_id: result for result in client.portal.call(go)}


def chat(message: dict, finish: str = "stop", **usage) -> httpx.Response:
    body = {"id": "c", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", **message}}]}
    if usage:
        body["usage"] = usage
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# MODEL-001 · MODEL-009: คำตอบว่างไม่ใช่คำตอบ
# ---------------------------------------------------------------------------
@respx.mock
def test_model_001_fails_when_the_model_says_nothing(client):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=chat({"content": "", "reasoning_content": "The user wants OK. I"},
                          finish="length"))
    result = run(client, "coding", "MODEL-001")["MODEL-001"]
    assert result.status == "fail", result
    assert "spent all 16 tokens reasoning" in result.notes
    assert "declare capabilities.reasoning" in result.notes, "ต้องบอกสิ่งที่ต้องไปแก้"


@respx.mock
@pytest.mark.parametrize("content", ["", "   \n", None], ids=["empty", "whitespace", "null"])
def test_model_001_fails_on_any_empty_reply(client, content):
    respx.post(f"{CODING}/v1/chat/completions").mock(return_value=chat({"content": content}))
    result = run(client, "coding", "MODEL-001")["MODEL-001"]
    assert result.status == "fail", result
    assert "empty reply (finish_reason=stop)" in result.notes


@respx.mock
def test_model_001_still_passes_when_the_model_answers(client):
    respx.post(f"{CODING}/v1/chat/completions").mock(return_value=chat({"content": "OK"}))
    result = run(client, "coding", "MODEL-001")["MODEL-001"]
    assert result.status == "pass", result
    assert "'OK'" in result.notes


@respx.mock
def test_a_declared_reasoning_model_is_given_room_to_think(client):
    """งบ 16 token การันตีว่าโมเดล reasoning ทุกตัวสอบตก — ซึ่งไม่ได้บอกอะไรเกี่ยวกับโมเดล"""
    muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=chat({"content": "OK", "reasoning_content": "They want OK."}))
    coding = respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=chat({"content": "OK"}))

    assert run(client, "muse-local", "MODEL-001")["MODEL-001"].status == "pass"
    assert run(client, "coding", "MODEL-001")["MODEL-001"].status == "pass"

    assert json.loads(muse.calls.last.request.content)["max_tokens"] == 1024
    assert json.loads(coding.calls.last.request.content)["max_tokens"] == 16, (
        "โมเดลที่ไม่ได้ประกาศ reasoning ไม่ควรได้งบเขียนยาวเพิ่ม")


@respx.mock
def test_a_reasoning_model_that_still_runs_out_is_told_what_to_raise(client):
    respx.post(f"{MUSE}/v1/chat/completions").mock(
        return_value=chat({"content": "", "reasoning_content": "Hmm. " * 400}, finish="length"))
    result = run(client, "muse-local", "MODEL-001")["MODEL-001"]
    assert result.status == "fail", result
    assert "spent all 1024 tokens reasoning" in result.notes
    assert "limits.max_output_tokens" in result.notes


@respx.mock
def test_model_009_fails_on_the_empty_text_the_translator_fills_in(client):
    """backend ไม่ตอบอะไร → ตัวแปลคืน `[{"type":"text","text":""}]` → เดิมผ่าน"""
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=chat({"content": "", "reasoning_content": "thinking"}, finish="length"))
    result = run(client, "coding", "MODEL-009")["MODEL-009"]
    assert result.status == "fail", result
    assert "stop_reason=max_tokens" in result.notes


@respx.mock
@pytest.mark.parametrize("message", [
    {"content": "OK"},
    {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "read_file", "arguments": '{"path": "a"}'}}]},
], ids=["text", "tool-call"])
def test_model_009_still_passes_on_a_real_reply(client, message):
    respx.post(f"{CODING}/v1/chat/completions").mock(return_value=chat(message))
    assert run(client, "coding", "MODEL-009")["MODEL-009"].status == "pass"


# ---------------------------------------------------------------------------
# MODEL-003: backend ที่มีหน้าต่างของตัวเอง
# ---------------------------------------------------------------------------
def backend_with_window(real_window: int, *, reports_usage: bool = True):
    """backend ที่นับ token แล้วปฏิเสธเมื่อเกินหน้าต่างจริงของมัน — แบบที่ vLLM ทำ

    ประโยคตัวเติมของชุดทดสอบคือ 10 token ใน tokenizer แบบ BPE ทั่วไป · นับจากคำว่า "fox"
    """
    def answer(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        tokens = prompt.count("fox") * 10
        if tokens > real_window:
            return httpx.Response(400, json={"error": {
                "message": f"This model's maximum context length is {real_window} tokens. "
                           f"However, you requested {tokens + 32} tokens ({tokens} in the "
                           "messages, 32 in the completion).",
                "type": "BadRequestError", "code": 400}})
        usage = {"prompt_tokens": tokens, "completion_tokens": 3} if reports_usage else {}
        return chat({"content": "many"}, **usage)

    return respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=answer)


@respx.mock
@pytest.mark.parametrize("slots", [2, 4], ids=["parallel-2", "parallel-4"])
def test_model_003_fails_when_the_window_is_over_declared(client, slots):
    """llama.cpp `--ctx-size 262144 --parallel N` ให้คำขอเดียว 262,144 / N — ไม่ใช่ 262,144"""
    backend_with_window(DECLARED // slots)
    result = run(client, "coding", "MODEL-003")["MODEL-003"]
    assert result.status == "fail", result
    assert f"maximum context length is {DECLARED // slots}" in result.notes, (
        "ตัวเลขของ backend ต้องไปถึงแอดมิน")


@respx.mock
def test_model_003_passes_when_the_backend_takes_more_than_half_the_window(client):
    route = backend_with_window(DECLARED)
    result = run(client, "coding", "MODEL-003")["MODEL-003"]
    assert result.status == "pass", result
    sent = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert sent.count("fox") * 10 > DECLARED / 2, "ต้องเติมเกินครึ่ง ไม่งั้นจับเกินจริง 2 เท่าไม่ได้"
    assert "60% of the declared 262,144-token window" in result.notes


@respx.mock
def test_model_003_does_not_vouch_for_a_window_the_backend_never_confirmed(client):
    """ไม่มี usage จาก backend = ไม่รู้ว่า prompt เข้าไปถึงเท่าไร

    เดิมขึ้น `0 prompt tokens accepted` เป็น pass
    """
    backend_with_window(DECLARED, reports_usage=False)
    result = run(client, "coding", "MODEL-003")["MODEL-003"]
    assert result.status == "degraded", result
    assert "reported no usage" in result.notes


def test_model_003_reports_no_answer_in_time_as_unproven_not_refused(client):
    """ช้าไม่ใช่ปฏิเสธ — backend ที่อ่าน prompt 157k ไม่ทันเวลายังไม่ได้บอกอะไรเรื่องหน้าต่าง

    ทั้ง pass และ fail จะเป็นคำตอบที่ไม่มีหลักฐานรองรับ
    """
    class NoAnswerInTime(httpx.ASGITransport):
        async def handle_async_request(self, request):
            if request.url.path == "/v1/chat/completions":
                raise httpx.ReadTimeout("no answer", request=request)
            return await super().handle_async_request(request)

    result = run(client, "coding", "MODEL-003", transport=NoAnswerInTime)["MODEL-003"]
    assert result.status == "degraded", result
    assert "neither confirmed nor refused" in result.notes
    assert "157,286-token prompt" in result.notes, "ต้องบอกว่าส่งอะไรไปถึงได้ช้า"
