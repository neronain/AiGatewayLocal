"""เมื่อเกตเวย์ลดเพดานคำตอบของผู้เรียก ต้องบอกเขา — `x-litegate-output-cap`

ตรวจ 2026-10-06 กับ prompt โค้ด 821,818 อักขระ (198,866 token จริงบน Gemma-4 = 76% ของ
หน้าต่าง 262,144): ตัวประมาณของเกตเวย์ให้ค่าเกิน 100% ของหน้าต่าง · client ขอ
`max_tokens=4000` · backend ได้ `max_tokens=256` · คำตอบ 200 กลับมาพร้อม header ชุดเดียวกับ
คำขอปกติทุกตัว — ไม่มีอะไรบอกว่าทำไมคำตอบถูกตัดกลางประโยค

การลดมีเหตุผล (backend ที่ตรวจ prompt + max_tokens จะปฏิเสธทั้งคำขอ) · สิ่งที่ผิดคือความเงียบ

    x-litegate-output-cap: granted=256; requested=4000; reason=context

เทสดูสองอย่างคู่กันเสมอ: ค่าที่ backend **ได้รับจริง** กับค่าใน header — header ที่บอกตัวเลข
ไม่ตรงกับของที่ส่งไปแย่กว่าไม่มี header
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

CODING = "http://dgx03:8000"   # coding · หน้าต่าง 262,144 · max_output_tokens 16,384
MUSE = "http://dgx01:8000"     # muse-local · max_output_tokens 8,192
CAP = 16_384
HEADER = "x-litegate-output-cap"

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "length",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
STREAM = (b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
          b"data: [DONE]\n\n")

# ตัวอักษรกับช่องว่างล้วน = 4 อักขระต่อ token ตามตัวประมาณ → 275,000 token
# = 105% ของหน้าต่าง: เกิน 100% แต่ไม่ถึง 115% ที่จะถูกปฏิเสธ — โซนที่เพดานถูกลดเหลือ 256
BORDERLINE = "word " * 220_000

PATHS = {"chat": "/v1/chat/completions", "messages": "/v1/messages",
         "responses": "/v1/responses"}
MAX = {"chat": "max_tokens", "messages": "max_tokens", "responses": "max_output_tokens"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _body(surface: str, text: str, **extra) -> dict:
    if surface == "responses":
        return {"model": "coding", "input": text, **extra}
    return {"model": "coding", "messages": [{"role": "user", "content": text}], **extra}


@pytest.fixture
def backend():
    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=STREAM,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=REPLY)

    with respx.mock:
        yield respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=answer)


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def _parse(header: str) -> dict[str, str]:
    return dict(part.strip().split("=", 1) for part in header.split(";"))


# ---------------------------------------------------------------------------
# โซน 100–115%: เคสที่ตรวจพบ — ทั้งสาม surface
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", PATHS)
def test_a_cap_lowered_for_the_context_window_is_announced(
        backend, client, member_key, surface):
    response = client.post(PATHS[surface], headers=auth(member_key),
                           json=_body(surface, BORDERLINE, **{MAX[surface]: 4000}))

    assert response.status_code == 200, response.text
    sent = _sent(backend)["max_tokens"]
    assert sent == 256, "ตัวควบคุม: นี่คือเคสที่เพดานถูกลดจริง"
    told = _parse(response.headers[HEADER])
    assert told == {"granted": str(sent), "requested": "4000", "reason": "context"}


def test_a_stream_is_told_in_its_headers_before_the_first_byte(backend, client, member_key):
    """สตรีมไม่มีที่ให้แจ้งใน body โดยไม่เพิ่ม event ที่ client ไม่รู้จัก — header มาก่อนเสมอ"""
    with client.stream("POST", PATHS["chat"], headers=auth(member_key),
                       json=_body("chat", BORDERLINE, max_tokens=4000, stream=True)) as response:
        told = _parse(response.headers[HEADER])
        response.read()
    assert response.status_code == 200
    assert told == {"granted": str(_sent(backend)["max_tokens"]),
                    "requested": "4000", "reason": "context"}


def test_a_caller_who_named_no_limit_is_told_too(backend, client, member_key):
    """ไม่ได้ขอ = คาดว่าจะได้เพดานของโมเดลตามที่แค็ตตาล็อกโชว์ · ได้ 256 ก็ต้องรู้เหมือนกัน"""
    response = client.post(PATHS["chat"], headers=auth(member_key),
                           json=_body("chat", BORDERLINE))
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == 256
    assert _parse(response.headers[HEADER]) == {
        "granted": "256", "requested": "default", "reason": "context"}


# ---------------------------------------------------------------------------
# เหตุผลอื่นที่ลดได้ — และตอนที่ไม่ได้ลดต้องไม่มี header
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", PATHS)
def test_asking_beyond_the_models_limit_is_announced(backend, client, member_key, surface):
    response = client.post(PATHS[surface], headers=auth(member_key),
                           json=_body(surface, "hi", **{MAX[surface]: 32_000}))
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == CAP
    assert _parse(response.headers[HEADER]) == {
        "granted": str(CAP), "requested": "32000", "reason": "model-limit"}


def test_sharing_the_cap_between_n_choices_is_announced(backend, client, member_key):
    response = client.post(PATHS["chat"], headers=auth(member_key),
                           json=_body("chat", "hi", n=4, max_tokens=8000))
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == CAP // 4
    assert _parse(response.headers[HEADER]) == {
        "granted": str(CAP // 4), "requested": "8000", "reason": "n"}


@pytest.mark.parametrize("surface", PATHS)
@pytest.mark.parametrize("limit", [None, 100, CAP], ids=["unset", "small", "exactly-the-cap"])
def test_an_untouched_request_carries_no_notice(backend, client, member_key, surface, limit):
    extra = {} if limit is None else {MAX[surface]: limit}
    response = client.post(PATHS[surface], headers=auth(member_key),
                           json=_body(surface, "hi", **extra))
    assert response.status_code == 200, response.text
    assert _sent(backend)["max_tokens"] == (limit or CAP)
    assert HEADER not in response.headers


def test_a_refused_request_carries_no_notice(backend, client, member_key):
    """header นี้พูดถึงคำตอบที่ได้ · คำขอที่ถูกปฏิเสธไม่มีคำตอบให้พูดถึง"""
    response = client.post(PATHS["chat"], headers=auth(member_key),
                           json=_body("chat", "word " * 260_000, max_tokens=4000))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert HEADER not in response.headers


# ---------------------------------------------------------------------------
# fallback เปลี่ยนโมเดล — header ต้องเป็นของตัวที่เสิร์ฟจริง
# ---------------------------------------------------------------------------
def test_the_notice_describes_the_model_that_actually_served(
        writable_config, client, member_key):
    """coding (เพดาน 16,384) ล่ม ล้มไป muse-local (เพดาน 8,192) · ขอ 12,000

    กับ coding ไม่มีอะไรถูกลด · กับ muse-local ถูกลดเหลือ 8,192 — header ต้องพูดถึงอย่างหลัง
    เพราะนั่นคือสิ่งที่เกิดกับคำขอนี้จริง
    """
    path = writable_config / "models" / "coding.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["routing"] = {"fallback": ["muse-local"]}
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    client.app.state.services.registry.reload()

    with respx.mock:
        respx.post(f"{CODING}/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("refused"))
        muse = respx.post(f"{MUSE}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=REPLY))
        response = client.post(PATHS["chat"], headers=auth(member_key),
                               json=_body("chat", "hi", max_tokens=12_000))

    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-served-by"] == "muse-local"
    assert _sent(muse)["max_tokens"] == 8192
    assert _parse(response.headers[HEADER]) == {
        "granted": "8192", "requested": "12000", "reason": "model-limit"}


def test_one_requests_notice_never_leaks_into_the_next(backend, client, member_key):
    """สมุดจดเป็นของคำขอ ไม่ใช่ของ worker — คำขอถัดไปบน connection เดิมต้องเริ่มเปล่า"""
    lowered = client.post(PATHS["chat"], headers=auth(member_key),
                          json=_body("chat", "hi", max_tokens=32_000))
    assert HEADER in lowered.headers
    plain = client.post(PATHS["chat"], headers=auth(member_key),
                        json=_body("chat", "hi", max_tokens=100))
    assert HEADER not in plain.headers
