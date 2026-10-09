"""`response_format` ที่ client ส่งมาผิดรูปนิดเดียว ต้องไปถึง backend ในรูปที่ใช้ได้ — หรือได้ 400

วัดกับ llama.cpp จริง 2026-10-09 (b11046 · ยิงตรง ไม่ผ่านเกตเวย์ · คำถาม "ตอบคำว่า hello" กับ
schema ที่บังคับ `{"color_code": "Q7"|"Z9"}` — ทำตาม schema หรือไม่จึงดูออกทันที):

    รูปที่ถูก                                   200 · {"color_code": "Q7"}
    strict อยู่ข้าง type                         200 · ทำตาม schema (llama.cpp ไม่อ่าน strict)
    json_schema.parameters แทน .schema          200 · {"answer": "hello"}    <- schema ถูกทิ้ง
    แบน (name/schema ข้าง type แบบ Responses)    200 · {"answer": "hello"}    <- schema ถูกทิ้ง
    type json_schema แต่ไม่มี schema             200 · {"answer": "hello"}
    มี json_schema แต่ไม่มี type                 200 · hello                  <- ทิ้งทั้งก้อน
    response_format เป็นสตริง "json_object"      200 · hello                  <- ทิ้งทั้งก้อน

ห้าแถวล่างคืออันตรายจริง: ตอบ 200 ทุกครั้ง client ที่ parse คำตอบตาม schema ของตัวเองพังทีหลัง
ในโค้ดของเขา โดยไม่มีอะไรบอกว่าเกตเวย์หรือ backend ไม่เคยเห็น schema นั้น

เทสทุกตัวยิงผ่าน HTTP จริงแล้วดู **body ที่ backend ได้รับ** — ไม่ได้เรียกตัวปรับรูปตรง ๆ
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

CODING = "http://dgx03:8000/v1/chat/completions"
PATH = "/v1/chat/completions"
HEADER = "x-litegate-adjusted"
MSG = [{"role": "user", "content": "Review the movie Dune 2"}]

MOVIE = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "rating": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["title", "rating", "summary"],
    "additionalProperties": False,
}
# สิ่งที่ LangChain `with_structured_output(method="json_schema")` ส่งจริง — ยกมาจากเทสของ
# OrcaRouter-Lite (tests/unit/test_json_schema_response_format.py: LANGCHAIN_MOVIE_REVIEW)
LANGCHAIN = {"type": "json_schema",
             "json_schema": {"name": "MovieReview", "strict": True, "schema": MOVIE}}
# สิ่งที่ `client.beta.chat.completions.parse(response_format=Order)` ของ OpenAI SDK ส่ง:
# schema จาก pydantic (title · $defs/$ref) · ลำดับคีย์ schema-name-strict เป็นของ SDK เอง
OPENAI_SDK_PARSE = {"type": "json_schema", "json_schema": {
    "schema": {
        "$defs": {"Item": {
            "properties": {"sku": {"title": "Sku", "type": "string"},
                           "qty": {"title": "Qty", "type": "integer"}},
            "required": ["sku", "qty"], "title": "Item", "type": "object",
            "additionalProperties": False}},
        "properties": {
            "customer": {"title": "Customer", "type": "string"},
            "items": {"items": {"$ref": "#/$defs/Item"}, "title": "Items", "type": "array"}},
        "required": ["customer", "items"], "title": "Order", "type": "object",
        "additionalProperties": False},
    "name": "Order", "strict": True}}
# strict:true ที่ไม่มี additionalProperties:false — OpenAI ปฏิเสธ · llama.cpp ไม่ต้องการ (ดูเทส)
OPEN_OBJECT = {"type": "object", "properties": {"title": {"type": "string"},
               "info": {"type": "object", "properties": {"year": {"type": "integer"}}}}}

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "{}"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
STREAM = (b'data: {"choices":[{"index":0,"delta":{"content":"{}"}}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
          b"data: [DONE]\n\n")


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def backend():
    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=STREAM,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=REPLY)

    with respx.mock:
        yield respx.post(CODING).mock(side_effect=answer)


@pytest.fixture
def cached(client):
    """เกตเวย์ที่เปิด response cache (ค่าตั้งต้นคือปิด)"""
    from app.core.responsecache import ResponseCache

    state = client.app.state.services
    state.response_cache = ResponseCache()
    yield client
    state.response_cache = None


def _post(client, key, response_format, **extra):
    return client.post(PATH, headers=auth(key), json={
        "model": "coding", "messages": MSG, "response_format": response_format, **extra})


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


def _canonical(name: str = "MovieReview", schema: dict = MOVIE, **inner) -> dict:
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, **inner}}


# ---------------------------------------------------------------------------
# รูปที่ซ่อมได้ — backend ต้องได้รูปมาตรฐาน และผู้เรียกต้องเห็นว่าถูกแก้อะไร
# ---------------------------------------------------------------------------
REPAIRED = {
    # คัดลอกจากเอกสาร LiteLLM/OpenAI รุ่นเก่า: strict อยู่ข้าง type
    "strict-beside-type": (
        {"type": "json_schema", "strict": True,
         "json_schema": {"name": "MovieReview", "schema": MOVIE}},
        _canonical(strict=True),
        ["response_format.strict->response_format.json_schema.strict"],
    ),
    # ชื่อฟิลด์ของ function calling หลุดมา — llama.cpp ตอบ 200 โดยไม่ใช้ schema เลย
    "parameters-for-schema": (
        {"type": "json_schema",
         "json_schema": {"name": "MovieReview", "strict": True, "parameters": MOVIE}},
        _canonical(strict=True),
        ["response_format.json_schema.parameters->response_format.json_schema.schema"],
    ),
    "both-at-once": (
        {"type": "json_schema", "strict": True,
         "json_schema": {"name": "MovieReview", "parameters": MOVIE}},
        _canonical(strict=True),
        ["response_format.strict->response_format.json_schema.strict",
         "response_format.json_schema.parameters->response_format.json_schema.schema"],
    ),
    # รูปของ Responses API (`text.format`) ถูกส่งมาทาง chat
    "flat": (
        {"type": "json_schema", "name": "MovieReview", "strict": True, "schema": MOVIE},
        _canonical(strict=True),
        ["response_format.name->response_format.json_schema.name",
         "response_format.strict->response_format.json_schema.strict",
         "response_format.schema->response_format.json_schema.schema"],
    ),
    # llama.cpp ทิ้งทั้งก้อนเมื่อไม่มี type (ตอบเป็นข้อความธรรมดา)
    "no-type": (
        {"json_schema": {"name": "MovieReview", "schema": MOVIE}},
        _canonical(),
        ['response_format.type="json_schema"'],
    ),
    # llama.cpp ไม่สนชื่อ แต่ vLLM กับ OpenAI บังคับ — ตัวแปลของ /v1/responses เติมค่านี้อยู่แล้ว
    "no-name": (
        {"type": "json_schema", "json_schema": {"schema": MOVIE}},
        _canonical(name="response"),
        ['response_format.json_schema.name="response"'],
    ),
    # ซ้ำสองที่: ตัวที่อยู่ถูกที่ชนะ ตัวที่อยู่ผิดที่ถูกทิ้ง — และบอก
    "strict-in-both-places": (
        {"type": "json_schema", "strict": True,
         "json_schema": {"name": "MovieReview", "strict": False, "schema": MOVIE}},
        _canonical(strict=False),
        ["response_format.strict->(dropped)"],
    ),
}


@pytest.mark.parametrize("case", REPAIRED)
def test_a_repairable_shape_reaches_the_backend_canonical(backend, client, member_key, case):
    given, expected, notes = REPAIRED[case]
    response = _post(client, member_key, given)

    assert response.status_code == 200, response.text
    assert _sent(backend)["response_format"] == expected
    # ผู้เรียกเห็นว่า payload ของเขาถูกเขียนใหม่ และเขียนตรงไหน
    assert response.headers[HEADER].split(", ") == notes


def test_the_adjustment_is_announced_on_a_stream_too(backend, client, member_key):
    """header ออกก่อนไบต์แรกของ body — การปรับรูปเกิดก่อนเปิดสตรีมจึงยังบอกทัน"""
    given, expected, notes = REPAIRED["parameters-for-schema"]
    with client.stream("POST", PATH, headers=auth(member_key), json={
            "model": "coding", "messages": MSG, "stream": True,
            "response_format": given}) as response:
        response.read()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _sent(backend)["response_format"] == expected
    assert response.headers[HEADER].split(", ") == notes


# ---------------------------------------------------------------------------
# ของที่ถูกอยู่แล้ว — ไม่แตะแม้แต่ลำดับคีย์ และไม่มี header
# ---------------------------------------------------------------------------
UNTOUCHED = {
    "langchain": LANGCHAIN,
    "openai-sdk-parse": OPENAI_SDK_PARSE,
    "json_object": {"type": "json_object"},
    "text": {"type": "text"},
    # ส่วนขยายของ llama.cpp: json_object ที่แนบ schema — วัดจริงแล้วถูกบังคับใช้
    "json_object-with-schema": {"type": "json_object", "schema": MOVIE},
    # ชนิดที่เกตเวย์ไม่รู้จัก (vLLM มี structural_tag) — backend ตัดสินเอง
    "a-type-we-do-not-know": {"type": "structural_tag", "format": {"type": "any_text"}},
    "empty-object": {},
    # strict:true + ไม่มี additionalProperties: schema เป็นของผู้เรียก เกตเวย์ไม่เติมให้
    "strict-without-additional-properties": {
        "type": "json_schema",
        "json_schema": {"name": "MovieReview", "strict": True, "schema": OPEN_OBJECT}},
    "description-and-unknown-keys": {
        "type": "json_schema",
        "json_schema": {"name": "MovieReview", "description": "a review", "schema": MOVIE,
                        "x-vendor": 1}},
}


@pytest.mark.parametrize("case", UNTOUCHED)
def test_a_valid_response_format_is_forwarded_untouched(backend, client, member_key, case):
    given = UNTOUCHED[case]
    response = _post(client, member_key, given)

    assert response.status_code == 200, response.text
    sent = _sent(backend)["response_format"]
    # เทียบเป็นข้อความ ไม่ใช่ dict: ลำดับคีย์ก็ต้องเหมือนเดิม
    assert json.dumps(sent) == json.dumps(given)
    assert HEADER not in response.headers, "ไม่ได้แก้อะไร ต้องไม่บอกว่าแก้"


def test_the_callers_schema_is_never_rewritten(backend, client, member_key):
    """OrcaRouter-Lite เติม additionalProperties:false และเขียน required ใหม่เมื่อ strict:true

    วัดกับ llama.cpp 2026-10-09: object ที่ไม่ระบุ additionalProperties **ปิดอยู่แล้ว** (ขอสามคีย์
    ได้คีย์เดียวที่ประกาศ · ผลเท่ากับใส่ false · ใส่ true ถึงจะเปิด) และคีย์นอก `required` ยังเป็น
    ตัวเลือกจริง — การเติมไม่ได้ช่วยอะไร แต่การเขียน `required` ใหม่เปลี่ยนความหมายของ schema
    """
    given = {"type": "json_schema", "strict": True, "json_schema": {
        "name": "MovieReview", "parameters": {**OPEN_OBJECT, "required": ["title"]}}}
    response = _post(client, member_key, given)

    assert response.status_code == 200, response.text
    schema = _sent(backend)["response_format"]["json_schema"]["schema"]
    assert schema == {**OPEN_OBJECT, "required": ["title"]}
    assert "additionalProperties" not in json.dumps(schema)


# ---------------------------------------------------------------------------
# รูปที่ซ่อมไม่ได้ — 400 ของเกตเวย์ที่บอกฟิลด์ ไม่ใช่ 200 ที่ schema หายไปเงียบ ๆ
# ---------------------------------------------------------------------------
BROKEN = {
    "no-schema-at-all": ({"type": "json_schema"}, "response_format.json_schema.schema"),
    "wrapper-without-schema": (
        {"type": "json_schema", "json_schema": {"name": "MovieReview", "strict": True}},
        "response_format.json_schema.schema"),
    "schema-is-null": (
        {"type": "json_schema", "json_schema": {"name": "MovieReview", "schema": None}},
        "response_format.json_schema.schema"),
    "schema-is-a-string": (
        {"type": "json_schema", "json_schema": {"name": "MovieReview", "schema": "object"}},
        "response_format.json_schema.schema"),
    "wrapper-is-a-string": (
        {"type": "json_schema", "json_schema": "MovieReview"}, "response_format.json_schema"),
    "name-is-a-number": (
        {"type": "json_schema", "json_schema": {"name": 7, "schema": MOVIE}},
        "response_format.json_schema.name"),
    "a-bare-string": ("json_object", "response_format"),
    "an-array": ([{"type": "json_object"}], "response_format"),
    "no-type-and-nothing-to-go-on": ({"format": "json"}, "response_format.type"),
    "type-is-a-number": ({"type": 1}, "response_format.type"),
}


@pytest.mark.parametrize("case", BROKEN)
def test_an_unrepairable_shape_is_refused_by_the_gateway(backend, client, member_key, case):
    given, param = BROKEN[case]
    response = _post(client, member_key, given)

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "INVALID_REQUEST"
    assert error["param"] == param
    assert f"'{param}'" in error["message"], error["message"]
    assert not backend.called, "คำขอที่ผิดต้องไม่ไปถึง backend"
    assert _usage_rows(client) == 0, "ต้องปฏิเสธก่อนจะมีอะไรถูกบันทึกหรือหักโควตา"


def test_the_refusal_shows_the_shape_that_is_expected(backend, client, member_key):
    response = _post(client, member_key, {"type": "json_schema", "json_schema": {"name": "x"}})

    message = response.json()["error"]["message"]
    assert '{"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}' in message


def test_a_broken_format_is_refused_before_a_stream_opens(backend, client, member_key):
    with client.stream("POST", PATH, headers=auth(member_key), json={
            "model": "coding", "messages": MSG, "stream": True,
            "response_format": {"type": "json_schema"}}) as response:
        response.read()

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert not backend.called


def test_null_means_not_asked(backend, client, member_key):
    """ตัวควบคุม — SDK บางตัวส่ง null แทนการละฟิลด์"""
    response = _post(client, member_key, None)
    assert response.status_code == 200, response.text
    assert HEADER not in response.headers


# ---------------------------------------------------------------------------
# แคชคำตอบ — key สร้างจาก payload ที่จะส่งจริง จึงเกิด *หลัง* การปรับรูป
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", ["strict-beside-type", "parameters-for-schema", "flat"])
def test_two_spellings_of_one_request_share_a_cached_answer(
        backend, cached, member_key, case):
    """backend ได้ไบต์เดียวกัน คำตอบจึงเป็นคำตอบเดียวกัน — ไม่มีเหตุให้ถามซ้ำ"""
    misspelt, canonical, _ = REPAIRED[case]

    first = _post(cached, member_key, canonical, temperature=0)
    assert first.headers["x-litegate-cache"] == "miss"
    second = _post(cached, member_key, misspelt, temperature=0)

    assert second.status_code == 200, second.text
    assert second.headers["x-litegate-cache"] == "hit"
    assert backend.call_count == 1
    # คำตอบมาจากแคช แต่คำขอนี้ก็ยังถูกเขียนใหม่ — ต้องบอกเหมือนเดิม
    assert HEADER in second.headers


def test_a_different_schema_is_a_different_question(backend, cached, member_key):
    """ตัวควบคุม — การปรับรูปต้องไม่ทำให้ schema สองตัวที่ต่างกันจริงมาชนกัน"""
    other = {**MOVIE, "required": ["title"]}
    given = {"type": "json_schema", "strict": True,
             "json_schema": {"name": "MovieReview", "parameters": MOVIE}}
    changed = {"type": "json_schema", "strict": True,
               "json_schema": {"name": "MovieReview", "parameters": other}}

    assert _post(cached, member_key, given, temperature=0).headers["x-litegate-cache"] == "miss"
    assert _post(cached, member_key, changed, temperature=0).headers["x-litegate-cache"] == "miss"
    assert backend.call_count == 2
    assert _sent(backend)["response_format"]["json_schema"]["schema"] == other
    # strict ต่างกันก็เป็นคนละคำขอ: backend บางตัวอ่านค่านี้
    loose = {"type": "json_schema", "strict": False,
             "json_schema": {"name": "MovieReview", "parameters": MOVIE}}
    assert _post(cached, member_key, loose, temperature=0).headers["x-litegate-cache"] == "miss"
    assert backend.call_count == 3
