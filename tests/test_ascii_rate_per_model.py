"""อัตราอักขระ ASCII ต่อ token ตั้งต่อโมเดลได้ — และถูกใช้ทุกที่ที่มีการประมาณ

ที่มา (2026-10-06): ค่ากลาง 4.0 (ตัวอักษร) กับ 1.0 (ตัวเลข/วรรคตอน) วัดจาก tokenizer ตัวเดียว
เทียบกับ tokenizer จริงของ Gemma-4 ค่าประมาณสูงกว่าจริง 1.24–1.44 เท่ากับโค้ดและ markdown
และ 2.06 เท่ากับนิยาม tool เป็น JSON · prompt โค้ด 228,569 token จริง (87% ของหน้าต่าง
262,144) ถูกปฏิเสธว่า "~308,283 tokens"

สองเรื่องที่ไฟล์นี้ยืนยัน:

1. **ไม่ตั้ง = เหมือนเดิมทุกตัวเลข** — ค่ากลางไม่ถูกแตะ จนกว่าผู้ดูแลจะวัดแล้วใส่เอง
2. **ตั้งแล้วทุกด่านนับด้วยตัวเลขเดียวกัน** — ด่าน context · `count_tokens` · ยอดที่บันทึกเมื่อ
   backend ไม่รายงาน usage · กฎ overflow · `auto` · ถ้าที่ใดที่หนึ่งยังใช้ค่ากลาง ด่านกับบิลจะ
   เห็นคำขอเดียวกันเป็นสองขนาด ซึ่งคือบั๊กที่ `wide_chars_per_token` เคยเป็นมาแล้ว
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml
from pydantic import ValidationError

from app.core.tokens import (
    CHARS_PER_TOKEN,
    SYMBOL_CHARS_PER_TOKEN,
    TokenRates,
    estimate_chars,
    estimate_text_tokens,
    rates_of,
)
from app.registry.schema import ModelDefinition

GEMMA = "http://dgx02:8000"     # gemma-vision · หน้าต่าง 262,144
WINDOW = 262_144

# ซอร์สโค้ดธรรมดา: ตัวอักษรปนวรรคตอนและตัวเลข — รูปของ prompt ที่ agent ส่งทั้งวัน
LINE = "    result = compute(value_1, other[2]) + offset  # keep the running total\n"
CODE = LINE * 11_500            # 862,500 อักขระ

# ค่าที่ผู้ดูแลจะได้จากการวัด tokenizer ของโมเดล (สมมติให้เทสนี้ — ของจริงต้องวัด)
MEASURED = {"ascii_chars_per_token": 4.6, "symbol_chars_per_token": 2.2}

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _model(**spec) -> ModelDefinition:
    return ModelDefinition.model_validate({
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": "probe", "display_name": "Probe"},
        "spec": {"upstream_model": "org/probe",
                 "limits": {"context_tokens": WINDOW, "max_output_tokens": 8192},
                 "endpoints": [{"name": "e", "server_type": "vllm",
                                "base_url": "http://e:8000"}],
                 **spec}})


def _split(text: str) -> tuple[int, int]:
    """(ตัวอักษร+ช่องว่าง, อย่างอื่น) — นับเองในเทส ไม่ยืมตัวนับของโค้ดที่กำลังทดสอบ"""
    letters = sum(1 for ch in text if ch.isalpha() or ch.isspace())
    return letters, len(text) - letters


# ---------------------------------------------------------------------------
# ตัวประมาณ
# ---------------------------------------------------------------------------
def test_unset_rates_give_exactly_the_old_numbers():
    letters, symbols = _split(CODE)
    old = int(letters / CHARS_PER_TOKEN) + int(symbols / SYMBOL_CHARS_PER_TOKEN)

    assert estimate_chars(CODE) == old
    assert estimate_chars(CODE, None) == old
    assert estimate_chars(CODE, TokenRates()) == old
    assert estimate_chars(CODE, rates_of(_model().spec)) == old
    # ตัวเลขเดี่ยวแบบเดิม (อัตรานอก ASCII) ยังรับได้ และไม่แตะส่วน ASCII
    assert estimate_chars(CODE, 3.86) == old


def test_a_measured_rate_is_used_for_each_kind_of_character():
    letters, symbols = _split(CODE)
    rates = rates_of(_model(**MEASURED).spec)

    assert estimate_chars(CODE, rates) == int(letters / 4.6) + int(symbols / 2.2)


def test_each_rate_moves_only_its_own_kind():
    prose = "the quick brown fox jumps over the lazy dog " * 100      # ไม่มีวรรคตอน
    digits = "1234567890" * 100                                        # ไม่มีตัวอักษร
    thai = "สวัสดี" * 100

    letters_only = TokenRates(letters=8.0)
    assert estimate_chars(prose, letters_only) == estimate_chars(prose) // 2
    assert estimate_chars(digits, letters_only) == estimate_chars(digits)
    assert estimate_chars(thai, letters_only) == estimate_chars(thai)

    symbols_only = TokenRates(symbols=2.0)
    assert estimate_chars(digits, symbols_only) == estimate_chars(digits) // 2
    assert estimate_chars(prose, symbols_only) == estimate_chars(prose)
    assert estimate_chars(thai, symbols_only) == estimate_chars(thai)


@pytest.mark.parametrize("bad", [0, -1, 0.1, 25, "fast", float("nan"), True])
def test_an_impossible_rate_falls_back_instead_of_breaking_the_maths(bad):
    """ค่าที่หลุดการตรวจของ schema มาได้ (เช่น object ที่สร้างในโค้ด) ต้องไม่ทำให้หารพัง
    และต้องไม่กลายเป็นการนับขาด"""
    for rates in (TokenRates(letters=bad), TokenRates(symbols=bad)):
        assert estimate_chars(CODE, rates) == estimate_chars(CODE)


@pytest.mark.parametrize("field", ["ascii_chars_per_token", "symbol_chars_per_token"])
@pytest.mark.parametrize("bad", [0, 0.2, -3, 20, 500, "fast"])
def test_the_registry_refuses_an_impossible_rate_at_load(field, bad):
    """ช่วงเดียวกับ `wide_chars_per_token` — ผิดตอนโหลด ไม่ใช่ตอนมีคำขอ"""
    _model(**{field: 3.5})      # ตัวควบคุม: ค่าปกติโหลดได้ — ที่ล้มข้างล่างจึงเป็นเพราะค่า
    with pytest.raises(ValidationError) as refused:
        _model(**{field: bad})
    assert [error["loc"][-1] for error in refused.value.errors()] == [field]


def test_the_profile_path_and_the_text_path_agree():
    """ด่าน context นับจาก profile · ด่านของ rerank นับจากตัวข้อความ — ต้องได้เลขเดียวกัน"""
    from app.core.multimodal import RequestProfile

    profile = RequestProfile()
    profile.add_text(CODE)
    rates = rates_of(_model(**MEASURED).spec)
    assert estimate_text_tokens(profile, rates) == estimate_chars(CODE, rates)


# ---------------------------------------------------------------------------
# ทางเดินจริง — ด่าน · count_tokens · บิล ต้องเห็นขนาดเดียวกัน
# ---------------------------------------------------------------------------
@pytest.fixture
def backend():
    with respx.mock:
        yield respx.post(f"{GEMMA}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=REPLY))


def _set_rates(config, client, rates: dict | None) -> None:
    path = config / "models" / "gemma-vision.yaml"
    document = yaml.safe_load(path.read_text())
    document["spec"]["protocols"].update({"anthropic": True})
    if rates:
        document["spec"].update(rates)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    client.app.state.services.registry.reload()


def _ask(client, key, **extra):
    return client.post("/v1/chat/completions", headers=auth(key), json={
        "model": "gemma-vision", "max_tokens": 4000,
        "messages": [{"role": "user", "content": CODE}], **extra})


def test_without_a_rate_the_gate_behaves_exactly_as_before(
        writable_config, backend, client, member_key):
    _set_rates(writable_config, client, None)

    response = _ask(client, member_key)

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert error["details"]["estimated_prompt_tokens"] == estimate_chars(CODE) == 310_500
    assert not backend.called


def test_with_a_measured_rate_the_same_prompt_is_served_with_its_full_output_budget(
        writable_config, backend, client, member_key):
    _set_rates(writable_config, client, MEASURED)

    response = _ask(client, member_key)

    assert response.status_code == 200, response.text
    assert json.loads(backend.calls.last.request.content)["max_tokens"] == 4000
    assert "x-litegate-output-cap" not in response.headers


def test_count_tokens_and_the_gate_use_the_same_number(
        writable_config, backend, client, member_key):
    """Claude Code ถาม count_tokens เพื่อตัดสินว่าจะย่อบทสนทนาเมื่อไร — ต้องเป็นเลขที่ด่านใช้"""
    _set_rates(writable_config, client, MEASURED)
    expected = estimate_chars(CODE, TokenRates(letters=4.6, symbols=2.2))

    counted = client.post("/v1/messages/count_tokens", headers=auth(member_key), json={
        "model": "gemma-vision", "messages": [{"role": "user", "content": CODE}]})

    assert counted.status_code == 200, counted.text
    assert counted.json()["input_tokens"] == expected
    assert expected < WINDOW < estimate_chars(CODE), "ตัวควบคุม: อัตรานี้เปลี่ยนคำตัดสินจริง"


def test_a_reply_without_backend_usage_is_billed_at_the_models_rate(
        writable_config, client, member_key):
    """backend ไม่รายงาน usage → เกตเวย์ประมาณเอง · ต้องเป็นอัตราเดียวกับที่ด่านเพิ่งใช้"""
    _set_rates(writable_config, client, MEASURED)
    silent = {k: v for k, v in REPLY.items() if k != "usage"}

    with respx.mock:
        respx.post(f"{GEMMA}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=silent))
        response = _ask(client, member_key)

    assert response.status_code == 200, response.text
    billed = response.json()["usage"]["litegate"]
    assert billed["accounting"] == "estimated"
    assert billed["text_input_tokens"] == estimate_chars(
        CODE, TokenRates(letters=4.6, symbols=2.2))


# ---------------------------------------------------------------------------
# ด่านระดับโมเดลอื่น ๆ ที่ใช้ตัวประมาณเดียวกัน
# ---------------------------------------------------------------------------
def test_routing_and_auto_see_the_prompt_at_the_models_own_rate():
    from app.core import auto, rules
    from app.core.multimodal import RequestProfile

    profile = RequestProfile()
    profile.add_text(CODE)
    by_default, measured = _model(), _model(**MEASURED)

    # กฎ overflow / fallback
    assert not rules._fits(by_default, profile)
    assert rules._fits(measured, profile)
    # auto
    assert not auto._fits(by_default, profile)
    assert auto._fits(measured, profile)


def test_output_the_gateway_relayed_is_metered_at_the_same_rates():
    """ฝั่ง output (เมื่อ backend ไม่รายงาน usage) ใช้ตัวนับอีกตัว — ต้องรับอัตราก้อนเดียวกัน

    ตัวนับนี้เคยรู้จักแค่ตัวเลขเดี่ยว · ส่ง TokenRates เข้าไปแล้วมันอ่านไม่ออกและถอยไปใช้
    ค่ากลางเงียบ ๆ: คำตอบภาษาไทยบนโมเดล 3.86 ถูกคิดเงินเกิน 2.4 เท่าโดยไม่มีอะไรฟ้อง
    """
    from app.core.tokens import OutputMeter

    thai = OutputMeter()
    thai.add("ก" * 386)
    assert thai.tokens(TokenRates(wide=3.86)) == 100
    assert thai.tokens(3.86) == 100
    assert thai.tokens() == int(386 / 1.6)

    code = OutputMeter()
    for line in [LINE] * 100:           # มาเป็นชิ้น ๆ เหมือน delta ของสตรีม
        code.add(line)
    rates = TokenRates(letters=4.6, symbols=2.2)
    assert code.tokens(rates) == estimate_chars(LINE * 100, rates)
    assert code.tokens() == estimate_chars(LINE * 100)
