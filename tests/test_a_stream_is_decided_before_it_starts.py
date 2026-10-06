"""สถานะ HTTP และ header ของ stream ตัดสินจากสิ่งที่เกิดก่อน event แรก — หลังจากนั้นต้องปิดให้จบ

สามเรื่องที่ตรวจพบ 2026-10-06 มีรากเดียวกัน: generator ของ stream เริ่มตอบ 200 ก่อนจะรู้ว่า
มีเครื่องไหนรับคำขอนี้ได้จริง

**ช่องเต็มที่ด่านจริง** · `router.acquire()` คือด่าน (ใบจองในตัวนับที่ทุก worker ใช้ร่วม) ส่วน
`select()` ดูแค่คำใบ้ของ process ตัวเอง · acquire ถูกเรียกใน generator หลังส่ง 200 และอยู่นอก
`except GatewayError` — ช่องที่ worker อื่นถืออยู่จึงกลายเป็น `RuntimeError: Caught handled
exception, but response already started` · header 200 แล้วสายตาย ไม่มี event error · แถว usage
`success/200` · โควตาถูกหักหนึ่งคำขอ · โมเดลสำรองที่ตั้งไว้ไม่ถูกลอง

**สายไป backend ขาดหลัง 200** · `httpx.ReadTimeout`/`RemoteProtocolError` ตอนไล่อ่าน body ตก
ไปที่ `except Exception` ซึ่ง `return` เฉย ๆ — ผู้เรียกได้คำตอบครึ่งเดียวที่จบเหมือนจบปกติ
และ backend ที่ตอบ 200 แล้วเงียบจนหมดเวลาได้ `HTTP 200 body=''` ทั้งที่เครื่องสำรองว่างอยู่

**header โกหก** · `x-litegate-served-by` ถูกตั้งก่อน stream สลับไปโมเดลสำรอง

`TestClient` กับ mock ที่ส่งจนจบเสมอไม่เคยเดินเส้นทางเหล่านี้ · backend ในไฟล์นี้ตายกลางทาง
เงียบจนหมดเวลา และช่องถูกถือโดย "worker อีกตัว" ผ่านตัวนับจริง
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.realistic_backends import (
    CODING,
    DONE,
    MUSE,
    ROLE,
    SPARE,
    STOP,
    SURFACE_NAMES,
    VllmLike,
    add_spare,
    another_worker_holds,
    auth,
    edit,
    ended_normally,
    endpoint_health,
    hang_up_on,
    quota_used,
    read_stream,
    request_for,
    slot_state,
    streaming,
    terminal_error,
    text_of,
    usage_chunk,
    usage_rows,
    words,
)

GOOD = (ROLE, *words(3), STOP, usage_chunk(7, 3), DONE)


@pytest.fixture
def two_machines(writable_config):
    add_spare(writable_config)
    return writable_config


@pytest.fixture
def muse_falls_back_to_coding(writable_config):
    edit(writable_config, "muse-local",
         lambda d: d["spec"].__setitem__("routing", {"fallback": ["coding"]}))
    return writable_config


def _error_code(response) -> str:  # noqa: ANN001
    error = response.json()["error"]
    return error.get("code") or error.get("type")


# ── ช่องเต็มที่ด่านจริง ────────────────────────────────────────────────────────
@respx.mock
@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_slot_held_by_another_worker_is_a_real_429(client, member_key, surface, stream):
    backend = respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=VllmLike())
    another_worker_holds(client, "coding")

    path, body = request_for(surface, stream=stream)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 429, response.text
    assert _error_code(response) == "CONCURRENCY_LIMIT_EXCEEDED"
    assert response.headers["retry-after"] == "5"
    assert not backend.called
    rows = usage_rows(client)
    assert len(rows) == 1, "ต้องมีแถวให้ตามหาได้ว่าใครโดน 429"
    assert (rows[0]["status"], rows[0]["http_status"], rows[0]["error_code"]) == (
        "error", 429, "CONCURRENCY_LIMIT_EXCEEDED")
    assert (rows[0]["input"], rows[0]["output"]) == (0, 0)
    used = quota_used(client)
    assert used["requests"] == 0 and used["input_tokens"] == 0, (
        "ไม่มี backend ไหนได้เห็นคำขอนี้ — การถูกปฏิเสธไม่ใช่การใช้งาน"
    )


@respx.mock
@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("surface", ["chat", "messages"])
def test_a_full_slot_falls_back_like_any_other_refusal(
    muse_falls_back_to_coding, client, member_key, surface, stream
):
    """ถูกปฏิเสธที่ acquire ต้องได้สิ่งเดียวกับถูกปฏิเสธที่ select: โมเดลสำรองก่อน แล้วค่อย 429"""
    muse = respx.post(url__startswith=MUSE).mock(side_effect=VllmLike())
    coding = respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=VllmLike(pieces=3))
    another_worker_holds(client, "muse-local")

    path, body = request_for(surface, "muse-local", stream=stream)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 200, response.text
    assert coding.called and not muse.called
    assert response.headers["x-litegate-model"] == "muse-local"
    assert response.headers["x-litegate-served-by"] == "coding", (
        "header ต้องบอกตัวที่เสิร์ฟจริง — stream เคยบอกชื่อตัวที่ขอ"
    )
    assert response.headers["x-litegate-endpoint"] == "dgx03"
    row = usage_rows(client)[-1]
    assert (row["status"], row["endpoint"]) == ("success", "dgx03")
    assert slot_state(client, "coding") == (0, 0), "ช่องของตัวสำรองต้องถูกคืน"


@respx.mock
def test_a_stream_that_fell_back_names_the_model_that_answered(
    muse_falls_back_to_coding, client, member_key
):
    """เครื่องของ alias ที่ขอล่มทั้งหมด → ตัวสำรองตอบ → header ต้องเป็นชื่อตัวสำรอง"""
    respx.post(url__startswith=MUSE).mock(side_effect=httpx.ConnectError("refused"))
    respx.post(f"{CODING}/v1/chat/completions").mock(side_effect=VllmLike(pieces=3))

    response, events = read_stream(client, member_key, "chat", model="muse-local")

    assert response.status_code == 200 and "word2" in text_of("chat", events)
    assert response.headers["x-litegate-served-by"] == "coding"
    assert response.headers["x-litegate-model"] == "muse-local"
    assert usage_rows(client)[-1]["endpoint"] == "dgx03"


# ── สายไป backend ขาดหลัง 200 ──────────────────────────────────────────────────
BREAKS = {
    "peer-closed": (httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body"),
        "UPSTREAM_UNAVAILABLE", 503),
    "read-timeout": (httpx.ReadTimeout(""), "UPSTREAM_TIMEOUT", 504),
}


@respx.mock
@pytest.mark.parametrize("how", BREAKS)
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_backend_that_dies_mid_answer_ends_the_stream_with_an_error(
    two_machines, client, member_key, surface, how
):
    failure, code, http_status = BREAKS[how]
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(ROLE, *words(2), failure))
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(*GOOD))

    response, events = read_stream(client, member_key, surface)

    assert response.status_code == 200       # เริ่มตอบไปแล้ว — ความล้มเหลวอยู่ในสาย
    assert "word1" in text_of(surface, events)
    assert not ended_normally(surface, events), "คำตอบครึ่งเดียวต้องไม่จบเหมือนจบปกติ"
    error = terminal_error(surface, events)
    assert error is not None, [e for e, _ in events]
    assert error["message"]
    assert not spare.called, "ผู้เรียกเห็นคำตอบไปแล้ว — เล่นซ้ำจากต้นคือให้เขาอ่านสองรอบ"
    row = usage_rows(client)[-1]
    assert (row["status"], row["error_code"], row["http_status"]) == ("error", code, http_status)
    assert endpoint_health(client)["total_failures"] == 1
    assert slot_state(client, "coding") == (0, 0)


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_silence_after_the_200_fails_over_while_nothing_was_sent(
    two_machines, client, member_key, surface
):
    """header 200 แล้วเงียบจนหมดเวลา — รูปของ vLLM ที่งานล้น · เดิมได้ `HTTP 200 body=''`"""
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(httpx.ReadTimeout("")))
    spare = respx.post(f"{SPARE}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(*GOOD))

    response, events = read_stream(client, member_key, surface)

    assert spare.called
    assert response.status_code == 200 and ended_normally(surface, events)
    assert "word2" in text_of(surface, events)
    assert response.headers["x-litegate-endpoint"] == "spare"
    assert response.headers["x-litegate-failed-over"] == "dgx03"
    row = usage_rows(client)[-1]
    assert (row["status"], row["endpoint"], row["output"]) == ("success", "spare", 3)
    assert endpoint_health(client)["total_failures"] == 1


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_silence_after_the_200_with_nowhere_to_go_is_a_real_504(client, member_key, surface):
    respx.post(f"{CODING}/v1/chat/completions").mock(
        side_effect=lambda request: streaming(httpx.ReadTimeout("")))

    path, body = request_for(surface, stream=True)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 504, response.text
    assert _error_code(response) == "UPSTREAM_TIMEOUT"
    row = usage_rows(client)[-1]
    assert (row["status"], row["http_status"], row["output"]) == ("error", 504, 0)


@respx.mock
@pytest.mark.parametrize("surface", SURFACE_NAMES)
def test_a_backend_error_before_the_first_event_keeps_its_status(client, member_key, surface):
    """ล้มก่อนมีอะไรถึงผู้เรียก = สถานะจริง ให้ SDK retry เองได้ ไม่ใช่ 200 ที่มี error ข้างใน"""
    respx.post(f"{CODING}/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"error": {"message": "engine died"}}))

    path, body = request_for(surface, stream=True)
    response = client.post(path, headers=auth(member_key), json=body)

    assert response.status_code == 502, response.text
    assert _error_code(response) == "UPSTREAM_ERROR"
    assert usage_rows(client)[-1]["status"] == "error"


@respx.mock
def test_hanging_up_while_waiting_for_the_first_token_frees_the_slot(client, member_key):
    """การรอ token แรกเกิดก่อน response เริ่ม — ถ้าไม่เฝ้าสายเอง Esc ตอนโมเดลอ่าน prompt
    จะถือช่องเดียวของ llama.cpp ไว้จนกว่า token แรกจะมา"""
    script = {}

    def thinking(request: httpx.Request) -> httpx.Response:
        script["response"] = streaming(30.0, *GOOD)   # โมเดลยังอ่าน prompt อยู่
        return script["response"]

    respx.post(f"{MUSE}/v1/chat/completions").mock(side_effect=thinking)

    seen = hang_up_on(client, member_key, "chat", "muse-local", after_frames=0)

    assert seen["raised"] is None and seen["frames"] == 0, seen
    assert script["response"].stream.closed, "สายไป backend ต้องถูกปิด ให้มันเลิกทำงาน"
    assert slot_state(client, "muse-local") == (0, 0)
    row = usage_rows(client)[-1]
    assert (row["status"], row["http_status"], row["error_code"], row["output"]) == (
        "aborted", 499, "CLIENT_CLOSED_REQUEST", 0)


# ── เส้นทางที่ไม่ใช่ chat ──────────────────────────────────────────────────────
EMBED_YAML = """
apiVersion: litegate.dev/v1
kind: Model
metadata:
  alias: embed
  display_name: Embedding
  visibility: member
spec:
  upstream_model: Qwen/Qwen3-Embedding-8B
  purpose: [embedding]
  limits: {context_tokens: 32768, max_output_tokens: 16}
  capabilities: {chat: false, streaming: false, embedding: true}
  protocols: {openai: false, embeddings: true}
  endpoints:
    - name: primary
      server_type: vllm
      base_url: http://dgx05:8000
      priority: 200
      max_concurrency: 1
      protocols: {openai: false, embeddings: true}
    - name: standby
      server_type: vllm
      base_url: http://dgx06:8000
      priority: 100
      max_concurrency: 1
      protocols: {openai: false, embeddings: true}
"""
EMBEDDING = {"object": "list", "model": "up", "data": [
    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "usage": {"prompt_tokens": 3, "total_tokens": 3}}


@pytest.fixture
def embedding_model(writable_config):
    (writable_config / "models" / "embed.yaml").write_text(EMBED_YAML, encoding="utf-8")
    return writable_config


@respx.mock
def test_embeddings_try_the_next_machine_when_the_slot_is_taken(
    embedding_model, client, member_key
):
    primary = respx.post("http://dgx05:8000/v1/embeddings").mock(
        return_value=httpx.Response(200, json=EMBEDDING))
    standby = respx.post("http://dgx06:8000/v1/embeddings").mock(
        return_value=httpx.Response(200, json=EMBEDDING))
    another_worker_holds(client, "embed", 0)
    ask = {"model": "embed", "input": "hello"}

    served = client.post("/v1/embeddings", headers=auth(member_key), json=ask)
    assert served.status_code == 200, served.text
    assert standby.called and not primary.called
    assert served.headers["x-litegate-endpoint"] == "standby"

    another_worker_holds(client, "embed", 1)
    refused = client.post("/v1/embeddings", headers=auth(member_key), json=ask)
    assert refused.status_code == 429
    last = usage_rows(client)[-1]
    assert (last["status"], last["error_code"]) == ("error", "CONCURRENCY_LIMIT_EXCEEDED")
