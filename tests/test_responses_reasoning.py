"""ความคิดของโมเดล reasoning บน /v1/responses — ต้องไปถึง Codex ไม่ใช่หายที่ตัวแปล

backend ที่พูด chat completions คืนความคิดแยกมาใน `reasoning_content` (vLLM รุ่นใหม่ใช้
ชื่อ `reasoning`) · ตัวแปล Responses เคยอ่านแค่ `content` กับ `tool_calls` โมเดลที่ใช้
งบ output หมดไปกับการคิดจึงออกมาเป็น `output: []` กับ `status: "incomplete"` — HTTP 200
คิดเงินเต็ม และไม่มีอะไรบอกว่าโมเดลทำงานไปแล้วทั้งก้อน · ระหว่างคิด stream ก็เงียบสนิท
ซึ่ง client ที่มี idle timeout อ่านว่าสายหลุด (ตัวแปล Anthropic แก้เรื่องเดียวกันใน 2eb489a)

Responses API มีที่ให้ใส่ตรง ๆ: output item ชนิด `reasoning` · ความคิดดิบอยู่ใน
`content[].reasoning_text` ส่วน `summary` เว้นว่าง เพราะเราไม่ได้สรุปอะไร — เอาความคิดดิบ
ไปใส่ช่อง summary คือบอกว่ามันเป็นสิ่งที่มันไม่ใช่

ทุกเทสยิงผ่าน /v1/responses จริง โดยมี respx เป็น backend
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

CHAT = "http://dgx03:8000/v1/chat/completions"
THOUGHT = "ผู้ใช้ถามเรื่องผลรวม ต้องบวกทีละตัว"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def completion(message: dict, finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1_700_000_000,
        "model": "backend-name",
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 64, "total_tokens": 75},
    }


def ask(client, key: str) -> dict:
    response = client.post(
        "/v1/responses", headers=auth(key), json={"model": "coding", "input": "1+1"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def stream(client, key: str, *chunks: dict) -> tuple[list[str], list[dict]]:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    respx.post(CHAT).mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    with client.stream(
        "POST", "/v1/responses", headers=auth(key),
        json={"model": "coding", "input": "1+1", "stream": True},
    ) as response:
        assert response.status_code == 200
        raw = "".join(response.iter_text())
    events = [line[7:] for line in raw.splitlines() if line.startswith("event: ")]
    payloads = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
    return events, payloads


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields}]}


def finish(reason: str) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 64}}


# ── ไม่ stream ──────────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_a_model_that_only_thought_does_not_come_back_empty(client, member_key, field):
    """เคสที่รายงานมา: คิดจนหมดงบ ไม่มีคำตอบ · vLLM ใช้ชื่อฟิลด์มาแล้วสองชื่อ"""
    respx.post(CHAT).mock(return_value=httpx.Response(
        200, json=completion({"content": None, field: THOUGHT}, finish="length")))

    body = ask(client, member_key)

    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}
    assert body["output_text"] == ""
    assert [item["type"] for item in body["output"]] == ["reasoning"], body["output"]
    item = body["output"][0]
    assert item["content"] == [{"type": "reasoning_text", "text": THOUGHT}]
    assert item["summary"] == [], "เราไม่ได้สรุป — ช่อง summary ต้องว่าง แต่ต้องมี (Codex บังคับ)"
    assert item["id"].startswith("rs_")


@respx.mock
def test_the_thought_comes_before_the_answer_and_stays_out_of_output_text(client, member_key):
    respx.post(CHAT).mock(return_value=httpx.Response(
        200, json=completion({"content": "2", "reasoning_content": THOUGHT})))

    body = ask(client, member_key)

    assert body["status"] == "completed"
    assert [item["type"] for item in body["output"]] == ["reasoning", "message"]
    assert body["output"][1]["content"][0]["text"] == "2"
    assert body["output_text"] == "2", "output_text คือคำตอบ ไม่ใช่ความคิด"


@respx.mock
def test_a_model_that_does_not_reason_gets_no_reasoning_item(client, member_key):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=completion({"content": "2"})))

    assert [item["type"] for item in ask(client, member_key)["output"]] == ["message"]


@respx.mock
def test_a_filtered_answer_is_not_reported_as_running_out_of_tokens(client, member_key):
    respx.post(CHAT).mock(return_value=httpx.Response(
        200, json=completion({"content": "ขอ"}, finish="content_filter")))

    body = ask(client, member_key)

    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "content_filter"}


# ── stream (ทางที่ Codex ใช้จริง) ───────────────────────────────────────────
@respx.mock
def test_thinking_is_streamed_as_it_happens_not_swallowed(client, member_key):
    events, payloads = stream(
        client, member_key,
        delta(reasoning_content="ผู้ใช้ถามเรื่องผลรวม "),
        delta(reasoning_content="ต้องบวกทีละตัว"),
        finish("length"),
    )

    assert events == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.reasoning_text.delta",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.output_item.done",
        "response.incomplete",
    ]
    assert [p["sequence_number"] for p in payloads] == list(range(len(payloads)))

    added, done = payloads[2]["item"], payloads[6]["item"]
    assert added["type"] == "reasoning" and added["summary"] == []
    assert done["id"] == added["id"]
    assert done["content"] == [{"type": "reasoning_text", "text": THOUGHT}]
    assert "".join(p["delta"] for p in payloads[3:5]) == THOUGHT
    assert payloads[5]["text"] == THOUGHT
    assert {p["item_id"] for p in payloads[3:6]} == {added["id"]}

    final = payloads[-1]["response"]
    assert final["status"] == "incomplete"
    assert final["incomplete_details"] == {"reason": "max_output_tokens"}
    assert final["output"] == [done], "สิ่งที่สรุปตอนจบต้องเป็น item เดียวกับที่ stream ไปแล้ว"
    assert final["output_text"] == ""


@respx.mock
def test_thought_answer_and_tool_call_each_get_their_own_slot(client, member_key):
    """output_index ต้องไม่ชนกัน และทุก item ที่เปิดต้องปิดที่ช่องเดิมด้วย id เดิม

    เดิมข้อความปิดที่ `output_index: 0` ตายตัว — ถูกตราบที่ข้อความเป็น item แรกเสมอ
    ซึ่งไม่จริงอีกแล้วเมื่อมี item ความคิดมาก่อน
    """
    events, payloads = stream(
        client, member_key,
        delta(reasoning_content=THOUGHT),
        delta(content="กำลัง"),
        delta(content="ดูให้"),
        delta(tool_calls=[{"index": 0, "id": "call_1",
                           "function": {"name": "add", "arguments": '{"a":1,'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '"b":1}'}}]),
        finish("tool_calls"),
    )

    assert events[-1] == "response.completed"
    assert [p["sequence_number"] for p in payloads] == list(range(len(payloads)))

    opened = {p["output_index"]: p["item"] for p in payloads
              if p["type"] == "response.output_item.added"}
    closed = {p["output_index"]: p["item"] for p in payloads
              if p["type"] == "response.output_item.done"}
    assert [opened[i]["type"] for i in sorted(opened)] == ["reasoning", "message", "function_call"]
    assert sorted(opened) == sorted(closed) == [0, 1, 2]
    assert {i: item["id"] for i, item in opened.items()} == {
        i: item["id"] for i, item in closed.items()
    }

    # ทุก event ย่อยต้องอ้างช่องของ item ตัวเอง ไม่ใช่ของตัวข้าง ๆ
    index_of = {item["id"]: i for i, item in opened.items()}
    scoped = [p for p in payloads if "item_id" in p]
    assert scoped and all(p["output_index"] == index_of[p["item_id"]] for p in scoped)

    # item หนึ่งต้องปิดก่อนตัวถัดไปเปิด — เปิดซ้อนกันไม่ได้
    order = [(p["type"].rsplit(".", 1)[1], p["output_index"]) for p in payloads
             if p["type"] in ("response.output_item.added", "response.output_item.done")]
    assert order[:4] == [("added", 0), ("done", 0), ("added", 1), ("done", 1)]

    final = payloads[-1]["response"]
    assert final["output"] == [closed[0], closed[1], closed[2]]
    assert final["output_text"] == "กำลังดูให้"
    assert closed[1]["content"][0]["text"] == "กำลังดูให้"
    assert closed[2]["arguments"] == '{"a":1,"b":1}'
    assert closed[2]["call_id"] == "call_1"


@respx.mock
def test_a_plain_answer_streams_exactly_as_before(client, member_key):
    events, payloads = stream(client, member_key, delta(content="2"), finish("stop"))

    assert events == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert payloads[-1]["response"]["output"] == [payloads[7]["item"]]
