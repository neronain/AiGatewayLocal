"""แคชคำตอบต้องคืนคำตอบของ *คำขอนั้น จากโมเดลนั้น* เท่านั้น

ตรวจ 2026-10-06 (แคชเปิดอยู่ · `coding` มี `routing.fallback: [muse-local]`):

    คำขอที่ 1  coding ล่ม → muse-local ตอบ "ANSWER FROM muse-local"   (served-by: muse-local)
    คำขอที่ 2  coding กลับมาแล้ว → ได้ "ANSWER FROM muse-local"
               พร้อม `x-litegate-served-by: coding` · `x-litegate-cache: hit`

key ถูกสร้างก่อนวง failover (จาก coding) แต่คำตอบถูกเก็บหลังวง (จาก muse-local) · header บอก
ว่า coding ตอบ ทั้งที่เนื้อคำตอบมาจากอีกโมเดล และจะเป็นแบบนั้นไปอีก 5 นาที

ทุกเทสในไฟล์นี้ดูที่ **เนื้อคำตอบ** กับ **จำนวนครั้งที่ backend ถูกเรียก** — ไม่ได้ดูว่า key
หน้าตาเป็นอย่างไร
"""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml

from tests.conftest import png_data_url

CODING = "http://dgx03:8000/v1/chat/completions"
CODING_B = "http://dgx04:8000/v1/chat/completions"
MUSE = "http://dgx01:8000/v1/chat/completions"
GEMMA = "http://dgx02:8000/v1/chat/completions"


def reply(text: str, **choice) -> dict:
    return {"id": "c", "object": "chat.completion", "model": "up",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}, **choice}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def edit(config, alias: str, change) -> None:
    path = config / "models" / f"{alias}.yaml"
    document = yaml.safe_load(path.read_text())
    change(document["spec"])
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


@pytest.fixture
def cached(client):
    """เกตเวย์ที่เปิด response cache (ค่าตั้งต้นคือปิด)"""
    from app.core.responsecache import ResponseCache

    state = client.app.state.services
    state.response_cache = ResponseCache()
    yield client
    state.response_cache = None


ASK = {"model": "coding", "temperature": 0,
       "messages": [{"role": "user", "content": "refactor this function"}]}


def _ask(client, key, **change):
    return client.post("/v1/chat/completions", headers=auth(key), json={**ASK, **change})


def _text(response) -> str:
    return response.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# เคสที่ตรวจพบ: คำตอบของโมเดลสำรองอยู่ใต้ key ของโมเดลที่ขอ
# ---------------------------------------------------------------------------
@respx.mock
def test_a_fallback_models_answer_does_not_outlive_the_outage(
        writable_config, cached, member_key):
    edit(writable_config, "coding",
         lambda spec: spec.__setitem__("routing", {"fallback": ["muse-local"]}))
    cached.app.state.services.registry.reload()

    coding = respx.post(CODING).mock(side_effect=httpx.ConnectError("refused"))
    muse = respx.post(MUSE).mock(
        return_value=httpx.Response(200, json=reply("ANSWER FROM muse-local")))

    during = _ask(cached, member_key)
    assert during.status_code == 200, during.text
    assert _text(during) == "ANSWER FROM muse-local"
    assert during.headers["x-litegate-served-by"] == "muse-local"

    # เครื่องของ coding กลับมา
    coding.mock(side_effect=None,
                return_value=httpx.Response(200, json=reply("ANSWER FROM coding")))
    after = _ask(cached, member_key)

    assert _text(after) == "ANSWER FROM coding", "ต้องไม่ได้คำตอบของโมเดลสำรองจากแคช"
    assert after.headers["x-litegate-served-by"] == "coding"
    assert after.headers["x-litegate-cache"] == "miss"
    assert muse.call_count == 1

    # และคำตอบของ coding เองยังถูกแคชตามปกติ — ไม่ได้ปิดแคชทิ้งไปทั้งอัน
    again = _ask(cached, member_key)
    assert again.headers["x-litegate-cache"] == "hit"
    assert _text(again) == "ANSWER FROM coding"
    assert again.headers["x-litegate-served-by"] == "coding"


@respx.mock
def test_an_answer_from_a_machine_running_other_weights_is_not_cached(
        writable_config, cached, member_key):
    """เครื่องสำรองของ alias เดียวกันที่ชี้ไปคนละ weights (`upstream_model` ของ endpoint)

    key บรรยายคำขอที่ส่งให้เครื่องแรก · คำตอบจากคนละ weights ไม่ใช่คำตอบของ key นั้น
    """
    def add_standby(spec):
        standby = dict(spec["endpoints"][0])
        standby.update({"name": "dgx04", "base_url": "http://dgx04:8000", "priority": 50,
                        "upstream_model": "someone/Other-Quantisation", "api_key_env": ""})
        spec["endpoints"].append(standby)

    edit(writable_config, "coding", add_standby)
    cached.app.state.services.registry.reload()

    primary = respx.post(CODING).mock(side_effect=httpx.ConnectError("refused"))
    standby = respx.post(CODING_B).mock(
        return_value=httpx.Response(200, json=reply("FROM OTHER WEIGHTS")))
    assert _text(_ask(cached, member_key)) == "FROM OTHER WEIGHTS"

    primary.mock(side_effect=None,
                 return_value=httpx.Response(200, json=reply("FROM THE NAMED WEIGHTS")))
    after = _ask(cached, member_key)

    assert _text(after) == "FROM THE NAMED WEIGHTS"
    assert after.headers["x-litegate-cache"] == "miss"
    assert standby.call_count == 1


@respx.mock
def test_failing_over_to_an_identical_machine_is_still_cached(
        writable_config, cached, member_key):
    """ตัวควบคุม: เครื่องสำรองที่รัน weights ชุดเดียวกัน ส่งคำขอเดียวกันเป๊ะ — คำตอบคือของ key"""
    def add_twin(spec):
        twin = dict(spec["endpoints"][0])
        twin.update({"name": "dgx04", "base_url": "http://dgx04:8000", "priority": 50,
                     "api_key_env": ""})
        spec["endpoints"].append(twin)

    edit(writable_config, "coding", add_twin)
    cached.app.state.services.registry.reload()

    respx.post(CODING).mock(side_effect=httpx.ConnectError("refused"))
    twin = respx.post(CODING_B).mock(
        return_value=httpx.Response(200, json=reply("SAME WEIGHTS")))

    assert _ask(cached, member_key).headers["x-litegate-cache"] == "miss"
    second = _ask(cached, member_key)
    assert second.headers["x-litegate-cache"] == "hit"
    assert _text(second) == "SAME WEIGHTS"
    assert twin.call_count == 1


# ---------------------------------------------------------------------------
# สิ่งที่ไม่ใช่คำตอบ ต้องไม่ถูกเก็บ
# ---------------------------------------------------------------------------
NOT_AN_ANSWER = {
    "error-with-200": {"error": {"message": "model is loading", "code": 503}},
    "no-choices": {"id": "c", "object": "chat.completion", "choices": []},
    "cut-before-finishing": reply("half an ans", finish_reason=None),
}


@pytest.mark.parametrize("case", NOT_AN_ANSWER)
@respx.mock
def test_a_reply_that_is_not_a_finished_answer_is_never_cached(cached, member_key, case):
    backend = respx.post(CODING).mock(
        return_value=httpx.Response(200, json=NOT_AN_ANSWER[case]))
    _ask(cached, member_key)

    backend.mock(return_value=httpx.Response(200, json=reply("THE REAL ANSWER")))
    recovered = _ask(cached, member_key)

    assert recovered.headers.get("x-litegate-cache") == "miss"
    assert _text(recovered) == "THE REAL ANSWER"
    assert backend.call_count == 2


@respx.mock
def test_an_upstream_error_is_never_cached(cached, member_key):
    backend = respx.post(CODING).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad prompt"}}))
    assert _ask(cached, member_key).status_code >= 400

    backend.mock(return_value=httpx.Response(200, json=reply("THE REAL ANSWER")))
    recovered = _ask(cached, member_key)
    assert recovered.status_code == 200
    assert _text(recovered) == "THE REAL ANSWER"
    assert backend.call_count == 2


# ---------------------------------------------------------------------------
# key ครอบทุกอย่างที่เปลี่ยนคำตอบได้ — วัดจาก backend ถูกเรียกกี่ครั้ง
# ---------------------------------------------------------------------------
SYSTEM = {"role": "system", "content": "answer in French"}
USER = ASK["messages"][0]
SCHEMA = {"type": "json_schema", "json_schema": {"name": "a", "schema": {"type": "object"}}}

CHANGES_THE_ANSWER = {
    "another-question": {"messages": [{"role": "user", "content": "something else"}]},
    "a-system-prompt": {"messages": [SYSTEM, USER]},
    "max_tokens": {"max_tokens": 50},
    "max_completion_tokens": {"max_completion_tokens": 50},
    "stop": {"stop": ["\n"]},
    "seed": {"seed": 7},
    "top_p": {"top_p": 0.5},
    "response_format": {"response_format": SCHEMA},
    "reasoning_effort": {"reasoning_effort": "high"},
    "chat_template_kwargs": {"chat_template_kwargs": {"enable_thinking": False}},
    "logit_bias": {"logit_bias": {"50256": -100}},
    "a-field-nobody-has-heard-of-yet": {"future_sampler": {"mode": "x"}},
}


@pytest.mark.parametrize("case", CHANGES_THE_ANSWER)
@respx.mock
def test_a_parameter_that_can_change_the_answer_is_part_of_the_key(cached, member_key, case):
    backend = respx.post(CODING).mock(return_value=httpx.Response(200, json=reply("A")))

    assert _ask(cached, member_key).headers["x-litegate-cache"] == "miss"
    changed = _ask(cached, member_key, **CHANGES_THE_ANSWER[case])

    assert changed.status_code == 200, changed.text
    assert changed.headers["x-litegate-cache"] == "miss", (
        "คำขอที่ต่างกันจริงต้องไม่ได้คำตอบของอีกคำขอ")
    assert backend.call_count == 2
    # และตัวมันเองซ้ำแล้วต้อง hit — ไม่ใช่ว่าฟิลด์นี้ทำให้แคชไม่ได้เลย
    assert _ask(cached, member_key,
                **CHANGES_THE_ANSWER[case]).headers["x-litegate-cache"] == "hit"


@pytest.mark.parametrize("extra", [{"user": "u-42"}, {"metadata": {"trace": "t1"}}],
                         ids=["user", "metadata"])
@respx.mock
def test_a_field_that_cannot_change_the_answer_does_not_split_the_cache(
        cached, member_key, extra):
    backend = respx.post(CODING).mock(return_value=httpx.Response(200, json=reply("A")))
    assert _ask(cached, member_key).headers["x-litegate-cache"] == "miss"
    assert _ask(cached, member_key, **extra).headers["x-litegate-cache"] == "hit"
    assert backend.call_count == 1


@respx.mock
def test_two_different_images_are_two_different_questions(cached, member_key):
    backend = respx.post(GEMMA).mock(return_value=httpx.Response(200, json=reply("A")))

    def look_at(url: str):
        return cached.post("/v1/chat/completions", headers=auth(member_key), json={
            "model": "gemma-vision", "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": url}}]}]})

    assert look_at(png_data_url(8, 8)).headers["x-litegate-cache"] == "miss"
    assert look_at(png_data_url(16, 16)).headers["x-litegate-cache"] == "miss"
    assert look_at(png_data_url(8, 8)).headers["x-litegate-cache"] == "hit"
    assert backend.call_count == 2


@respx.mock
def test_a_key_that_may_not_use_the_model_gets_nothing_from_the_cache(cached, member_key):
    """สิทธิ์ถูกตรวจก่อนถึงแคช — คำตอบที่คนอื่นทำให้อุ่นไว้ไม่ใช่ทางเข้าโมเดลที่ถูกกัน"""
    backend = respx.post(CODING).mock(return_value=httpx.Response(200, json=reply("A")))
    assert _ask(cached, member_key).status_code == 200

    admin = auth(cached.admin_key)
    owner = cached.get("/admin/users", headers=admin).json()
    owner = next(u for u in (owner.get("data") or owner) if u["external_id"] == "6412345678")
    narrow = cached.post("/admin/api-keys", headers=admin, json={
        "user_id": owner["id"], "name": "muse-only", "models": ["muse-local"]}).json()["api_key"]

    refused = _ask(cached, narrow)
    assert refused.status_code == 403, refused.text
    assert backend.call_count == 1
