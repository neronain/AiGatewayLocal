"""อัตราอักขระนอก ASCII ต่อ token ต้องตั้งต่อโมเดลได้ ไม่ใช่ค่าเดียวทั้งระบบ

วัดจริงบนฟลีต 2026-09-28: ภาษาไทยกระจาย 1.80–3.86 อักขระ/token ระหว่างโมเดล
(ต่างกัน 2.1 เท่า) เพราะ pre-tokenizer ของบางตัวไม่นับเครื่องหมายประสม จึงฉีก
"ที่" เป็น "ท" + "ี่" ส่วนตัวที่ถูกต้องนับเป็น token เดียว

ค่าเดียว 1.6 ให้ผล: เดาเกิน +10% กับโมเดลที่ฉีก (พอรับได้) แต่ +137% กับตัวที่
ถูกต้อง · ค่านี้คุมด่าน context และยอดโควตา ผู้ใช้ภาษาไทยบนกลุ่มหลังจึงชนเพดานที่
~42% ของความจุจริงและถูกคิดเงินเกิน 2.4 เท่า
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.core.modeltest import measure_wide_rate
from app.core.tokens import (
    WIDE_CHARS_PER_TOKEN,
    RequestProfile,
    estimate_chars,
    estimate_text_tokens,
    wide_rate,
)

THAI = "สวัสดีครับ วันนี้อากาศดีมาก"


# ── wide_rate: ตัวกรองค่าที่เป็นไปไม่ได้ ──────────────────────────────────────
def test_no_rate_falls_back_to_the_shared_constant():
    assert wide_rate(None) == WIDE_CHARS_PER_TOKEN


def test_a_measured_rate_is_used_as_is():
    assert wide_rate(3.86) == 3.86


@pytest.mark.parametrize("bad", [0, -1, 0.1, 25.0, float("nan"), "สาม", None])
def test_impossible_rates_fall_back_instead_of_breaking_the_maths(bad):
    """0 หรือติดลบทำให้หารพัง · สูงเกินจริงคือการนับต่ำกว่าจริง ซึ่งคือช่องโหว่โควตา"""
    assert wide_rate(bad) == WIDE_CHARS_PER_TOKEN


# ── ค่าที่ต่างกันต้องให้ผลต่างกันจริง ────────────────────────────────────────
def test_a_higher_rate_counts_fewer_tokens():
    low = estimate_chars(THAI, 1.8)
    high = estimate_chars(THAI, 3.86)
    assert high < low, "อัตราสูง = อักขระต่อ token เยอะ = token น้อยลง"


def test_the_measured_rate_lands_near_what_the_backend_really_counts():
    """โมเดลที่ tokenizer ถูกต้องนับ 27 อักขระนี้ได้ 7 token (วัดจริงบน spark-worker)"""
    assert 6 <= estimate_chars(THAI, 3.86) <= 9
    # ค่าสำรองเดิมให้ผลเกินเท่าตัวกับโมเดลกลุ่มนี้ — นั่นคือเหตุผลของทั้งงานนี้
    assert estimate_chars(THAI, None) >= 14


def test_ascii_is_untouched_by_the_rate():
    """อัตรานี้คุมเฉพาะอักขระนอก ASCII — อังกฤษต้องได้เท่าเดิมทุกค่า"""
    en = "The quick brown fox jumps over the lazy dog"
    assert estimate_chars(en, 1.6) == estimate_chars(en, 3.86) == estimate_chars(en, None)


def test_profile_path_honours_the_rate_too():
    p = RequestProfile(text_chars=len(THAI), text_wide_chars=sum(1 for c in THAI if ord(c) > 127),
                       text_symbol_chars=0)
    assert estimate_text_tokens(p, 3.86) < estimate_text_tokens(p, None)


# ── measure_wide_rate: ต้องอ่านได้ทั้งสองรูปแบบคำตอบ ─────────────────────────
def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")


@pytest.mark.asyncio
async def test_it_reads_the_vllm_shape():
    """vLLM ตอบ {"count": N}"""
    def handler(request):
        body = json.loads(request.content)
        assert "prompt" in body or "content" in body
        return httpx.Response(200, json={"count": 40})
    async with _client(handler) as c:
        rate, why = await measure_wide_rate(c, "http://x", "m")
    assert why == ""
    assert rate and 1.0 < rate < 20.0


@pytest.mark.asyncio
async def test_it_reads_the_llamacpp_shape():
    """llama.cpp ตอบ {"tokens": [...]} — เคยพลาดเพราะอ่านแต่ของ vLLM"""
    async with _client(lambda r: httpx.Response(200, json={"tokens": list(range(40))})) as c:
        rate, why = await measure_wide_rate(c, "http://x", "m")
    assert why == ""
    assert rate and 1.0 < rate < 20.0


@pytest.mark.asyncio
async def test_more_tokens_means_a_lower_rate():
    """backend ที่ฉีกคำจะคืน token เยอะกว่า → อัตราต้องต่ำกว่า"""
    rates = {}
    for tag, count in (("ฉีก", 80), ("ปกติ", 40)):
        async with _client(lambda r, n=count: httpx.Response(200, json={"count": n})) as c:
            rates[tag], _ = await measure_wide_rate(c, "http://x", "m")
    assert rates["ฉีก"] < rates["ปกติ"]


@pytest.mark.asyncio
async def test_a_backend_without_tokenize_does_not_break_the_probe():
    """วัดไม่ได้ = ใช้ค่าสำรอง ไม่ใช่ทำให้ทั้ง probe ล้ม"""
    async with _client(lambda r: httpx.Response(404)) as c:
        rate, why = await measure_wide_rate(c, "http://x", "m")
    assert rate is None and why


@pytest.mark.asyncio
async def test_a_nonsense_answer_is_rejected_not_stored():
    """count = 0 จะทำให้หารด้วยศูนย์ · count ที่น้อยจนหักส่วน ASCII แล้วติดลบก็ใช้ไม่ได้"""
    for payload in ({"count": 0}, {"count": -5}, {"count": 1}, {"nope": 1}):
        async with _client(lambda r, p=payload: httpx.Response(200, json=p)) as c:
            rate, why = await measure_wide_rate(c, "http://x", "m")
        assert rate is None, payload


# ── ลำดับสำคัญ: วัดก่อนขั้นที่ทำ backend ล้มได้ ─────────────────────────────
#
# เคสจริง 2026-09-28: probe Nemotron แล้วขั้นทดสอบ tools ทำให้ EngineCore ตาย
# ทั้งคอนเทนเนอร์ (vLLM ตอบ 500 แล้ว force kill) · ตอนนั้นการวัดอยู่ท้ายฟังก์ชัน
# จึงได้ "วัดไม่ได้" ทั้งที่ /tokenize ตอบปกติจนถึงวินาทีก่อนหน้า
def test_the_rate_is_measured_before_the_probes_that_can_kill_a_backend():
    import inspect

    from app.core import modeltest

    src = inspect.getsource(modeltest.probe_backend)
    measure = src.index("measure_wide_rate(client")
    for risky, label in ((src.index('"/v1/chat/completions"'), "chat"),
                         (src.index("tools ->"), "tools"),
                         (src.index("png_data_url()"), "vision")):
        assert measure < risky, f"ต้องวัดก่อนขั้น {label}"


def test_a_failed_measurement_is_reported_not_swallowed():
    """วัดไม่ได้ต้องขึ้นใน notes — ไม่ใช่เงียบแล้วปล่อยให้คนเดาว่าทำไมไม่มีค่า"""
    import inspect

    from app.core import modeltest

    src = inspect.getsource(modeltest.probe_backend)
    assert "วัดอัตราไม่ได้" in src


# ── ทุกที่ที่ประมาณ token ต้องใช้อัตราของโมเดลตัวที่กำลังถูกถามถึง ───────────────
#
# 0667ea7 ให้ด่าน context กับยอดโควตาใช้ `wide_chars_per_token` ของโมเดลแล้ว · ตรวจ
# 2026-10-05 พบว่ายังเหลือสี่จุดที่ใช้ค่าสำรองกลาง (1.6) — กฎ routing · `model="auto"` ·
# ตอนจบสตรีมของ /v1/messages · /v1/messages/count_tokens · ผลคือระบบเดียวกันนับคำขอ
# เดียวกันได้สองขนาด แล้วสองส่วนตัดสินสวนกัน
def _thai(chars: int) -> str:
    return "ก" * chars


def _registry(**models):
    """ทะเบียนจากโมเดลจริงใน config/ ที่แก้ limits / อัตรา / routing ตามที่เทสต้องการ"""
    from pathlib import Path

    from app.registry.schema import ModelDefinition
    from app.registry.store import RegistrySnapshot, load_snapshot

    base = load_snapshot(Path(__file__).resolve().parent.parent / "config")
    built = {}
    for alias, spec in models.items():
        data = base.get("coding").model_dump(mode="python")
        data["metadata"] = {**data["metadata"], "alias": alias}
        data["spec"] = {**data["spec"], "routing": {}, **spec}
        built[alias] = ModelDefinition.model_validate(data)
    return RegistrySnapshot(gateway=base.gateway, models=built)


def _profile_of(text: str):
    from app.core.multimodal import profile_openai_request
    from app.registry.schema import VisionPolicy

    return profile_openai_request(
        {"model": "x", "messages": [{"role": "user", "content": text}]}, VisionPolicy())


def test_overflow_is_not_triggered_for_a_prompt_the_model_itself_can_hold():
    """ไทย 100,000 อักขระบนโมเดลที่วัดได้ 3.86 = ~26k token — พอสำหรับหน้าต่าง 32,768

    เดิมกฎ routing นับด้วย 1.6 ได้ 62,500 แล้วส่งคำขอไปตัวใหญ่ ทั้งที่ด่าน context (ซึ่งใช้
    3.86) บอกว่าตัวเล็กรับได้ — ตัวใหญ่ถูกกินช่องโดยงานที่ไม่จำเป็นต้องใช้มัน
    """
    from app.core.rules import resolve_route

    snapshot = _registry(
        good={"limits": {"context_tokens": 32768}, "wide_chars_per_token": 3.86,
              "routing": {"overflow": "wide"}},
        wide={"limits": {"context_tokens": 262144}},
    )
    decision = resolve_route(
        snapshot, snapshot.get("good"), _profile_of(_thai(100_000)), "openai")
    assert decision.model.alias == "good", decision


def test_overflow_is_triggered_when_the_models_own_tokenizer_makes_the_prompt_too_long():
    """ทิศที่เจ็บกว่า: ไทย 50,000 อักขระบนโมเดลที่ tokenizer ฉีก (1.0) = 50k token

    กฎ routing เคยนับด้วย 1.6 ได้ 31,250 "พอดีหน้าต่าง 32,768" จึงไม่ส่งต่อ — แล้วด่าน
    context ซึ่งนับด้วย 1.0 ก็ตอบ 400 ให้คำขอที่มีโมเดลกว้างกว่ารอรับอยู่
    """
    from app.core.capability import validate_context_budget
    from app.core.errors import GatewayError
    from app.core.rules import resolve_route

    snapshot = _registry(
        torn={"limits": {"context_tokens": 32768}, "wide_chars_per_token": 1.0,
              "routing": {"overflow": "wide"}},
        wide={"limits": {"context_tokens": 262144}, "wide_chars_per_token": 1.0},
    )
    profile = _profile_of(_thai(50_000))
    with pytest.raises(GatewayError):
        validate_context_budget(snapshot.get("torn"), profile, None)   # ด่านจริงปฏิเสธตัวเล็ก

    decision = resolve_route(snapshot, snapshot.get("torn"), profile, "openai")
    assert decision.model.alias == "wide", "กฎต้องเห็นขนาดเดียวกับด่าน แล้วส่งต่อก่อนถึงด่าน"


def test_auto_keeps_a_model_whose_tokenizer_makes_the_prompt_fit():
    from app.core import auto
    from app.core.perf import PerfStore

    snapshot = _registry(
        good={"limits": {"context_tokens": 32768}, "wide_chars_per_token": 3.86})
    choice = auto.choose(list(snapshot.models.values()), profile=_profile_of(_thai(100_000)),
                         protocol="openai", perf=PerfStore())
    assert choice is not None and choice.model.alias == "good"


def test_auto_never_picks_a_model_the_context_gate_will_then_refuse():
    """`auto` เลือกตัวที่ด่านถัดไปปฏิเสธ = ผู้ใช้ได้ 400 จากคำขอที่ไม่ได้ระบุโมเดลด้วยซ้ำ"""
    from app.core import auto
    from app.core.capability import validate_context_budget
    from app.core.perf import PerfStore

    snapshot = _registry(
        torn={"limits": {"context_tokens": 32768}, "wide_chars_per_token": 1.0},
        wide={"limits": {"context_tokens": 262144}, "wide_chars_per_token": 1.0},
    )
    profile = _profile_of(_thai(50_000))
    choice = auto.choose(list(snapshot.models.values()), profile=profile,
                         protocol="openai", perf=PerfStore())
    assert choice.ranked == ("wide",), choice
    validate_context_budget(choice.model, profile, None)        # ต้องไม่โยน


CODING = "http://dgx03:8000"       # coding · wide_chars_per_token 1.89 · แปล /v1/messages
PROMPT = _thai(18_900)             # 10,000 token ที่อัตรา 1.89 · 11,812 ที่ค่าสำรอง 1.6


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_count_tokens_answers_with_the_models_own_rate(client, member_key):
    """Claude Code ใช้ตัวเลขนี้ตัดสินว่าจะย่อบทสนทนาเมื่อไร — ต้องตรงกับที่ด่าน context นับ"""
    answer = client.post("/v1/messages/count_tokens", headers=_auth(member_key), json={
        "model": "coding", "messages": [{"role": "user", "content": PROMPT}]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["input_tokens"] == 10_000


def test_an_anthropic_stream_without_backend_usage_is_billed_at_the_models_rate(
        client, member_key):
    """backend ไม่รายงาน usage ในสตรีม → เกตเวย์ประมาณเอง · ต้องประมาณด้วยอัตราของโมเดล

    ทางไม่สตรีมกับอีกสอง surface ทำถูกอยู่แล้ว — เหลือตอนจบสตรีมของ /v1/messages ที่เดียว
    ที่ยังใช้ 1.6 ผู้ใช้ภาษาไทยบน Claude Code จึงถูกหักโควตาเกิน 18% เฉพาะเมื่อสตรีม
    """
    import respx

    stream = b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    with respx.mock:
        respx.post(f"{CODING}/v1/chat/completions").mock(return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=stream))
        with client.stream("POST", "/v1/messages", headers=_auth(member_key), json={
                "model": "coding", "max_tokens": 16, "stream": True,
                "messages": [{"role": "user", "content": PROMPT}]}) as reply:
            assert reply.status_code == 200
            reply.read()

    rows = client.get("/admin/usage/quota", headers=_auth(client.admin_key)).json()["data"]
    used = next(row["used"] for row in rows if row["used"]["requests"])
    assert used["input_tokens"] == 10_000, used
