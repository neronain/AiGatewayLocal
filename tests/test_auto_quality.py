"""`model="auto"` มีแกนคุณภาพ — กลยุทธ์ `quality` กับ `balanced`

ที่มา (2026-10-09): `auto` เรียงตามความเร็วที่วัดได้อย่างเดียว · ฟลีตที่มีโมเดลเล็ก (เร็ว) กับ
โมเดลใหญ่ (เก่งกว่า) ให้คนคนเดียวกันใช้ได้ทั้งคู่ `auto` จึงส่งงานไปตัวเล็กเสมอ ไม่มีทางให้
ผู้ดูแลบอกว่า "งานที่ไม่ได้ระบุโมเดล ให้ตัวที่ดีกว่า"

สิ่งที่ไฟล์นี้ยืนยัน ผ่านตัวจัดอันดับจริง (`auto.choose`) และทางเดินคำขอจริง:

1. กลยุทธ์ตั้งต้นยังเป็น `fastest` — ตั้งคะแนนไว้เฉย ๆ ไม่เปลี่ยนอะไร
2. `quality` เลือกตัวคะแนนสูงสุด *ในบรรดาตัวที่รับคำขอนั้นได้* · ตัวที่ไม่มีคะแนนอยู่ท้ายแถว ไม่ถูกตัดทิ้ง
3. `balanced` ชั่งคุณภาพกับความเร็ว 2:1 — คุณภาพที่มากกว่า 1 คะแนน คุ้มกับความเร็วที่เสียไป 2%
   ของตัวเร็วสุด · ขอบของกติกานี้ถูกทดสอบตรงเส้น
4. `auto` ยังไม่ใช่ทางข้ามสิทธิ์ ไม่ว่ากลยุทธ์ไหน
5. หน้าพรีวิวโชว์ตัวเลขชุดเดียวกับที่ตัวจัดอันดับใช้ตัดสิน
"""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml

from app.core import auto
from app.core.multimodal import ImageRef, RequestProfile
from app.core.perf import MIN_SAMPLES, PerfStore
from app.registry.schema import ModelDefinition

CODING = "http://dgx03:8000"       # ไม่รับภาพ · 262,144
MUSE = "http://dgx01:8000"         # รับภาพ · 131,072
GEMMA = "http://dgx02:8000"        # รับภาพ · 262,144
BACKENDS = {"coding": CODING, "muse-local": MUSE, "gemma-vision": GEMMA}

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
ASK = [{"role": "user", "content": "สรุปเอกสารนี้ให้หน่อย"}]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# เครื่องมือ: โมเดลสองขนาด + สถิติความเร็วที่กำหนดเอง
# ---------------------------------------------------------------------------
def _model(alias: str, *, quality: int | None = None, context: int = 32768,
           vision: bool = False) -> ModelDefinition:
    spec: dict = {
        "upstream_model": f"org/{alias}",
        "limits": {"context_tokens": context, "max_output_tokens": 4096},
        "endpoints": [{"name": "e", "server_type": "vllm", "base_url": f"http://{alias}:8000",
                       "modalities": {"text": True, "image": vision}}],
    }
    if vision:
        spec["capabilities"] = {"vision": True}
        spec["modalities"] = {"input": ["text", "image"], "output": ["text"]}
    if quality is not None:
        spec["quality_score"] = quality
    return ModelDefinition.model_validate({
        "apiVersion": "litegate.dev/v1", "kind": "Model",
        "metadata": {"alias": alias, "display_name": alias}, "spec": spec})


def _measured(store: PerfStore | None = None, **tps: float) -> PerfStore:
    """สถิติเหมือนที่ทราฟฟิกจริงทิ้งไว้: MIN_SAMPLES คำขอ ความเร็วคงที่ → EWMA เท่ากับค่านั้นพอดี

    ชื่อ alias ที่มีขีดส่งเป็น `muse_local=` (ขีดล่างถูกแปลงกลับ)
    """
    store = store or PerfStore()
    for alias, speed in tps.items():
        for _ in range(MIN_SAMPLES):
            store.record(alias.replace("_", "-"), latency_ms=1200, ttft_ms=200,
                         output_tokens=int(speed))
    return store


def _text(chars: int = 200) -> RequestProfile:
    profile = RequestProfile()
    profile.add_text("a" * chars)
    return profile


def _with_image() -> RequestProfile:
    profile = _text()
    profile.modalities.add("image")
    profile.images.append(ImageRef(source="test", mime="image/png", size_bytes=10,
                                   url="", width=64, height=64))
    return profile


def _choose(models, perf, strategy=None, profile=None) -> auto.AutoChoice:
    extra = {} if strategy is None else {"strategy": strategy}
    choice = auto.choose(list(models), profile=profile or _text(), protocol="openai",
                         perf=perf, **extra)
    assert choice is not None
    return choice


# ตัวเล็กเร็ว 5 เท่า · ตัวใหญ่ได้คะแนนมากกว่า 50 — รูปของปัญหาที่งานนี้มีไว้แก้
SMALL = _model("small", quality=40)
LARGE = _model("large", quality=90)
FLEET = (SMALL, LARGE)
SPEEDS = {"small": 200.0, "large": 40.0}


# ---------------------------------------------------------------------------
# 1. ของเดิมไม่เปลี่ยน
# ---------------------------------------------------------------------------
def test_speed_only_still_picks_the_fast_model():
    """ตั้งคะแนนไว้แล้วแต่ไม่ได้เปลี่ยนกลยุทธ์ = พฤติกรรมเดิมทุกอย่าง"""
    perf = _measured(**SPEEDS)
    for choice in (_choose(FLEET, perf), _choose(FLEET, perf, "fastest")):
        assert choice.model.alias == "small"
        assert choice.ranked == ("small", "large")
        assert "200" in choice.reason, choice.reason


def test_roomiest_is_untouched():
    wide, narrow = _model("wide", quality=10, context=262144), _model("narrow", quality=99)
    assert _choose((narrow, wide), _measured(narrow=300.0), "roomiest").model.alias == "wide"


def test_a_strategy_this_version_does_not_know_is_treated_as_fastest():
    """ค่าที่เวอร์ชันใหม่กว่าเขียนไว้ (หลังถอยเวอร์ชัน) ต้องไม่ทำให้ `auto` ล้ม — ถอยไปของเดิม"""
    assert _choose(FLEET, _measured(**SPEEDS), "cheapest").ranked == ("small", "large")


# ---------------------------------------------------------------------------
# 2. quality
# ---------------------------------------------------------------------------
def test_quality_picks_the_higher_scored_model():
    choice = _choose(FLEET, _measured(**SPEEDS), "quality")
    assert choice.model.alias == "large"
    assert choice.ranked == ("large", "small")
    assert "90" in choice.reason, choice.reason


def test_quality_only_ranks_models_that_can_serve_the_request():
    """กรองด้วยข้อเท็จจริงก่อน แล้วค่อยเรียงตามคะแนน — ตัวคะแนนสูงสุดที่รับภาพไม่ได้ต้องไม่ถูกเลือก"""
    eyes = _model("eyes", quality=55, vision=True)
    blind_best = _model("blind-best", quality=95)
    eyes_low = _model("eyes-low", quality=20, vision=True)
    fleet = (blind_best, eyes_low, eyes)

    assert _choose(fleet, PerfStore(), "quality").model.alias == "blind-best"
    with_image = _choose(fleet, PerfStore(), "quality", _with_image())
    assert with_image.ranked == ("eyes", "eyes-low")


def test_quality_skips_a_better_model_whose_window_cannot_hold_the_prompt():
    tiny_best = _model("tiny-best", quality=95, context=4096)
    roomy = _model("roomy", quality=50, context=262144)
    long_prompt = _text(chars=200_000)

    assert _choose((roomy, tiny_best), PerfStore(), "quality").model.alias == "tiny-best"
    assert _choose((roomy, tiny_best), PerfStore(), "quality", long_prompt).ranked == ("roomy",)


def test_an_unscored_model_is_last_under_quality_even_when_it_is_the_fastest():
    newcomer = _model("newcomer")
    perf = _measured(newcomer=900.0, **SPEEDS)
    assert _choose((newcomer, *FLEET), perf, "quality").ranked == ("large", "small", "newcomer")


def test_an_unscored_model_is_still_chosen_when_it_is_the_only_one_that_fits():
    """ท้ายแถว ไม่ใช่ถูกตัดทิ้ง — ฟลีตมีไม่กี่ตัว ตัดแล้วอาจไม่เหลือใครรับคำขอนั้น"""
    only_eyes = _model("only-eyes", vision=True)
    for strategy in ("quality", "balanced"):
        choice = _choose((only_eyes, *FLEET), _measured(**SPEEDS), strategy, _with_image())
        assert choice.model.alias == "only-eyes", strategy
        assert choice.ranked == ("only-eyes",)


def test_equal_scores_are_settled_by_speed():
    slow, quick = _model("slow", quality=70), _model("quick", quality=70)
    perf = _measured(slow=30.0, quick=120.0)
    assert _choose((slow, quick), perf, "quality").ranked == ("quick", "slow")


@pytest.mark.parametrize("strategy", ["quality", "balanced"])
def test_with_no_scores_anywhere_the_new_strategies_rank_exactly_like_fastest(strategy):
    """เปลี่ยนกลยุทธ์ก่อนให้คะแนนโมเดลสักตัว ต้องไม่ได้ลำดับมั่ว ๆ — ได้ลำดับเดิม"""
    fleet = (_model("m1"), _model("m2"), _model("m3"))
    perf = _measured(m1=50.0, m3=150.0)               # m2 ยังไม่มีสถิติ
    assert _choose(fleet, perf, strategy).ranked == _choose(fleet, perf, "fastest").ranked \
        == ("m3", "m1", "m2")


# ---------------------------------------------------------------------------
# 3. balanced — คุณภาพ 2 ส่วน ความเร็ว 1 ส่วน
# ---------------------------------------------------------------------------
def _pair(large_quality: int, large_tps: float, small_quality: int = 60,
          small_tps: float = 100.0) -> tuple[auto.AutoChoice, dict[str, auto.Candidate]]:
    fleet = (_model("small", quality=small_quality), _model("large", quality=large_quality))
    choice = _choose(fleet, _measured(small=small_tps, large=large_tps), "balanced")
    return choice, {c.model.alias: c for c in choice.candidates}


def test_balanced_prefers_the_capable_model_when_the_quality_gap_is_wide():
    """90 กับ 40 ห่างกัน 50 คะแนน · ช้ากว่า 5 เท่า = เสียความเร็ว 80% ซึ่ง 'ราคา' คือ 40 คะแนน"""
    choice = _choose(FLEET, _measured(**SPEEDS), "balanced")
    assert choice.ranked == ("large", "small")


def test_balanced_prefers_the_fast_model_when_the_quality_gap_is_narrow():
    """70 กับ 40 ห่างกัน 30 คะแนน — ไม่พอจ่ายความเร็วที่เสียไป 80% (ต้อง 40)"""
    fleet = (_model("small", quality=40), _model("large", quality=70))
    assert _choose(fleet, _measured(**SPEEDS), "balanced").ranked == ("small", "large")


def test_balanced_on_the_line_one_point_or_one_token_decides():
    """ช้ากว่า 40% (60 เทียบ 100 tok/s) 'ราคา' คือ 20 คะแนนพอดี

    ตรงเส้น = เสมอ → ให้ตัวที่เร็วกว่า · เกินเส้นไป 1 คะแนน หรือเร็วขึ้น 1 tok/s → ตัวที่ดีกว่าชนะ
    """
    tie, numbers = _pair(large_quality=80, large_tps=60.0)
    assert numbers["large"].combined == numbers["small"].combined == 73.3
    assert tie.ranked == ("small", "large"), "เสมอกัน ต้องได้ตัวที่เร็วกว่า"

    assert _pair(large_quality=81, large_tps=60.0)[0].ranked == ("large", "small")
    assert _pair(large_quality=80, large_tps=61.0)[0].ranked == ("large", "small")
    assert _pair(large_quality=79, large_tps=60.0)[0].ranked == ("small", "large")


def test_balanced_numbers_are_the_stated_formula():
    """(2 × คะแนน + 100 × ความเร็วเทียบตัวเร็วสุด) ÷ 3 — ตรวจด้วยเลขที่คิดมือ"""
    _, numbers = _pair(large_quality=90, large_tps=25.0, small_quality=40, small_tps=100.0)
    assert numbers["small"].speed == 1.0 and numbers["large"].speed == 0.25
    assert numbers["small"].combined == round((2 * 40 + 100 * 1.0) / 3, 1) == 60.0
    assert numbers["large"].combined == round((2 * 90 + 100 * 0.25) / 3, 1) == 68.3
    assert auto.QUALITY_WEIGHT == 2 * auto.SPEED_WEIGHT


def test_balanced_before_any_traffic_ranks_by_quality():
    """เกตเวย์เพิ่งรีสตาร์ต (สถิติอยู่ในหน่วยความจำ) — ยังไม่มีหลักฐานเรื่องความเร็วของใครเลย"""
    choice = _choose(FLEET, PerfStore(), "balanced")
    assert choice.ranked == ("large", "small")
    assert all(c.speed_assumed and c.output_tps is None for c in choice.candidates)


def test_balanced_does_not_hold_missing_speed_samples_against_a_model():
    """ตัวเล็กมีสถิติ (มีคนเรียกตรง ๆ ทั้งวัน) ตัวใหญ่ยังไม่มี — ถ้าคิดว่า 'ไม่มีสถิติ = ช้าสุด'
    ตัวใหญ่จะไม่ถูกเลือก จึงไม่มีวันได้สถิติ แล้ว balanced ก็ค้างอยู่ที่ตัวเล็กตลอดไป

    คิดเสมือนเร็วเท่าตัวเร็วสุดไปก่อน: ถ้าเร็วสุดแล้วยังแพ้ก็ไม่ต้องวัด · ถ้าชนะก็ได้ถูกเลือกและ
    ถูกวัด — พอมีตัวเลขจริงก็ใช้ตัวเลขจริง
    """
    fleet = (_model("small", quality=60), _model("large", quality=90))
    perf = _measured(small=200.0)

    before = _choose(fleet, perf, "balanced")
    assert before.ranked == ("large", "small")
    large = next(c for c in before.candidates if c.model.alias == "large")
    assert large.output_tps is None and large.speed == 1.0 and large.speed_assumed

    # วัดแล้วช้ากว่า 10 เท่า: เสียความเร็ว 90% 'ราคา' 45 คะแนน แต่ห่างกันแค่ 30
    after = _choose(fleet, _measured(perf, large=20.0), "balanced")
    assert after.ranked == ("small", "large")
    large = next(c for c in after.candidates if c.model.alias == "large")
    assert large.output_tps == 20.0 and large.speed == 0.1 and not large.speed_assumed


def test_an_unscored_model_is_last_under_balanced_even_when_it_is_the_fastest():
    """คะแนนไม่ได้มาเองตามทราฟฟิกเหมือนความเร็ว — ไม่มีคนตั้งก็ไม่มีวันมี จึงไม่เดาแทนผู้ดูแล

    ผลข้างเคียงที่ตั้งใจ: โมเดลที่เพิ่งเพิ่มเข้าทะเบียนไม่แย่งงาน `auto` จนกว่าจะมีคนให้คะแนน
    """
    newcomer = _model("newcomer")
    choice = _choose((newcomer, *FLEET), _measured(newcomer=900.0, **SPEEDS), "balanced")
    assert choice.ranked[-1] == "newcomer"
    assert choice.candidates[-1].combined is None


# ---------------------------------------------------------------------------
# 4-5. ทางเดินจริง: สิทธิ์ · ค่าตั้งต้น · พรีวิว
# ---------------------------------------------------------------------------
@pytest.fixture
def backends():
    with respx.mock:
        yield {alias: respx.post(f"{url}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=REPLY)) for alias, url in BACKENDS.items()}


@pytest.fixture
def scored(writable_config):
    """ทะเบียนตัวอย่างที่ผู้ดูแลให้คะแนนแล้ว: coding ดีสุด · muse-local รองลงมา · gemma ยังไม่ให้

    คู่กับ `_traffic(muse_local=200, coding=40)`: fastest → muse-local · quality → coding ·
    balanced → coding (66.7 ต่อ 60.0)
    """
    for alias, score in (("coding", 90), ("muse-local", 40)):
        path = writable_config / "models" / f"{alias}.yaml"
        document = yaml.safe_load(path.read_text())
        document["spec"]["quality_score"] = score
        path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    return writable_config


def _set_strategy(client, strategy: str, key: str | None = None):
    return client.put("/admin/auto/strategy", headers=auth(key or client.admin_key),
                      json={"strategy": strategy})


def _auto(client, key: str, **body):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": "auto", "messages": ASK, **body})


def _traffic(client, **tps: float) -> None:
    _measured(client.app.state.services.perf, **tps)


def _member(client, external_id: str, **key_fields) -> str:
    headers = auth(client.admin_key)
    user = client.post("/admin/users", headers=headers,
                       json={"external_id": external_id, "role": "member"}).json()
    return client.post("/admin/api-keys", headers=headers,
                       json={"user_id": user["id"], "name": "k", **key_fields}).json()["api_key"]


def test_scores_alone_change_nothing_until_the_strategy_is_changed(
        scored, backends, client, member_key):
    """ระบบที่รันอยู่: อัปเดตแล้วให้คะแนนโมเดลไปก่อนได้ โดย `auto` ยังเลือกแบบเดิมจนกว่าจะสั่ง"""
    _traffic(client, muse_local=200.0, coding=40.0)

    response = _auto(client, member_key)

    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-served-by"] == "muse-local"
    assert backends["muse-local"].called and not backends["coding"].called


def test_the_quality_strategy_sends_auto_to_the_better_model(
        scored, backends, client, member_key, caplog):
    _traffic(client, muse_local=200.0, coding=40.0)
    assert _set_strategy(client, "quality").status_code == 200

    with caplog.at_level("INFO", logger="app.api.openai"):
        response = _auto(client, member_key)

    assert response.status_code == 200, response.text
    # ชื่อและเหตุผลไปโผล่ที่เดิม: header บอกตัวที่ตอบ · log บอกว่าเลือกเพราะอะไร
    assert response.headers["x-litegate-served-by"] == "coding"
    assert response.headers["x-litegate-model"] == "coding"
    assert backends["coding"].called and not backends["muse-local"].called
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("auto -> ")]
    assert said and "auto -> coding" in said[0] and "90" in said[0], said


def test_a_caller_not_permitted_the_best_model_never_gets_it(scored, backends, client):
    """`auto` ไม่ขยายสิทธิ์ — คะแนนสูงแค่ไหนก็ไม่ทำให้โมเดลที่กันไว้ถูกเลือกให้คนที่ไม่มีสิทธิ์"""
    narrow = _member(client, "narrow", models=["muse-local", "gemma-vision"])
    wide = _member(client, "wide")
    _traffic(client, muse_local=200.0, coding=40.0)

    for strategy in ("quality", "balanced"):
        assert _set_strategy(client, strategy).status_code == 200
        for route in backends.values():
            route.reset()

        response = _auto(client, narrow)
        assert response.status_code == 200, response.text
        assert response.headers["x-litegate-served-by"] == "muse-local", strategy
        assert not backends["coding"].called, f"{strategy}: coding ไม่อยู่ในสิทธิ์ของ key ใบนี้"

        # ตัวควบคุม: คนที่มีสิทธิ์ได้ coding จริง — เทสข้างบนไม่ได้ผ่านเพราะ coding ไม่เคยถูกเลือกเลย
        assert _auto(client, wide).headers["x-litegate-served-by"] == "coding", strategy


def test_the_quality_strategy_still_filters_by_what_the_request_needs(
        scored, backends, client, member_key):
    """coding คะแนนสูงสุดแต่รับภาพไม่ได้ — คำขอที่มีภาพต้องไปตัวที่ดีที่สุด *ที่รับภาพได้*"""
    from tests.conftest import png_data_url

    assert _set_strategy(client, "quality").status_code == 200
    image = {"type": "image_url", "image_url": {"url": png_data_url(8, 8)}}

    response = _auto(client, member_key, messages=[{"role": "user", "content": [image]}])

    assert response.status_code == 200, response.text
    assert response.headers["x-litegate-served-by"] == "muse-local"
    assert not backends["coding"].called


def test_only_an_admin_changes_the_strategy_and_only_to_one_that_exists(
        scored, client, member_key):
    assert _set_strategy(client, "quality", key=member_key).status_code == 403
    refused = _set_strategy(client, "smartest")
    assert refused.status_code == 400, refused.text
    assert "balanced" in refused.text, "ต้องบอกว่าเลือกอะไรได้บ้าง"

    preview = client.get("/admin/auto/preview", headers=auth(client.admin_key)).json()
    assert preview["strategy"] == "fastest", "ค่าที่ถูกปฏิเสธต้องไม่ถูกบันทึก"


def test_every_worker_reads_the_strategy_the_admin_chose(scored, backends, client, member_key):
    """เกตเวย์รัน 4 worker — การตั้งค่าที่อยู่ในหน่วยความจำของตัวที่รับคำสั่งจะใช้ได้แค่ 1 ใน 4 คำขอ

    process ที่สองเปิดจากฐานข้อมูลเดียวกัน: ไม่มีสถิติความเร็วเป็นของตัวเอง แต่ต้องรู้กลยุทธ์
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    assert _set_strategy(client, "quality").status_code == 200
    with TestClient(create_app()) as other:
        # ทราฟฟิกที่ worker ตัวนี้เห็นบอกว่า muse-local เร็วกว่า — ถ้ามันยังใช้ fastest จะได้ muse-local
        _traffic(other, muse_local=200.0, coding=40.0)
        preview = other.get("/admin/auto/preview", headers=auth(client.admin_key)).json()
        assert preview["strategy"] == "quality"
        assert _auto(other, member_key).headers["x-litegate-served-by"] == "coding"


def test_a_stored_strategy_this_version_does_not_know_falls_back_to_fastest(
        scored, backends, client, member_key):
    """ถอยเวอร์ชันหลังเวอร์ชันใหม่กว่าเขียนค่าที่เราไม่รู้จักไว้ — `auto` ต้องยังตอบ ด้วยของเดิม"""
    from app.db.models import AUTO_STRATEGY_KEY, GatewaySetting
    from app.db.session import session_scope

    async def write():
        async with session_scope() as session:
            session.add(GatewaySetting(key=AUTO_STRATEGY_KEY, value="cheapest"))

    client.portal.call(write)
    _traffic(client, muse_local=200.0, coding=40.0)

    assert _auto(client, member_key).headers["x-litegate-served-by"] == "muse-local"
    preview = client.get("/admin/auto/preview", headers=auth(client.admin_key)).json()
    assert preview["strategy"] == preview["configured_strategy"] == "fastest"


def test_the_strategy_change_is_in_the_audit_log(scored, client):
    """การตั้งค่าที่ย้ายงานของทั้งเกตเวย์ไปอีกโมเดล ต้องตอบได้ว่าใครเปลี่ยน จากอะไรเป็นอะไร"""
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope

    assert _set_strategy(client, "balanced").status_code == 200

    async def read():
        async with session_scope() as session:
            return list((await session.execute(
                select(AuditLog).where(AuditLog.action == "auto.strategy"))).scalars())

    rows = client.portal.call(read)
    assert len(rows) == 1
    assert rows[0].payload == {"strategy": "balanced", "previous": "fastest"}
    assert rows[0].actor_user_id


@pytest.mark.parametrize("strategy", ["fastest", "quality", "balanced"])
def test_preview_shows_the_numbers_the_decision_used(scored, client, strategy):
    """ตัวเลขในพรีวิว = ตัวเลขที่ตัวจัดอันดับใช้ ไม่ใช่สูตรที่เขียนซ้ำใน route

    เทียบสามทาง: (ก) พรีวิวผ่าน HTTP (ข) `auto.choose` เรียกตรงด้วยสถานะเดียวกัน (ค) คิดมือ
    """
    _traffic(client, muse_local=200.0, coding=40.0)          # gemma-vision ยังไม่มีสถิติ
    assert _set_strategy(client, strategy).status_code == 200

    preview = client.get("/admin/auto/preview?prompt_tokens=1000",
                         headers=auth(client.admin_key)).json()
    services = client.app.state.services
    profile = RequestProfile(text_chars=4000)
    decided = auto.choose(list(services.registry.snapshot.models.values()), profile=profile,
                          protocol="openai", perf=services.perf, strategy=strategy)

    assert preview["strategy"] == strategy
    assert preview["chosen"] == decided.model.alias
    assert preview["reason"] == decided.reason
    assert [row["alias"] for row in preview["ranked"]] == list(decided.ranked)
    for row, used in zip(preview["ranked"], decided.candidates, strict=True):
        assert row["quality_score"] == used.quality
        assert row["output_tps"] == used.output_tps
        assert row["speed"] == used.speed
        assert row["speed_assumed"] == used.speed_assumed
        assert row["combined"] == used.combined

    by_alias = {row["alias"]: row for row in preview["ranked"]}
    assert by_alias["coding"]["quality_score"] == 90
    assert by_alias["muse-local"]["output_tps"] == 200.0
    assert by_alias["gemma-vision"]["quality_score"] is None
    if strategy == "balanced":
        assert by_alias["coding"]["speed"] == 0.2
        assert by_alias["coding"]["combined"] == round((2 * 90 + 100 * 0.2) / 3, 1)
        assert by_alias["muse-local"]["combined"] == round((2 * 40 + 100 * 1.0) / 3, 1)
        assert by_alias["gemma-vision"]["combined"] is None
    expected = {"fastest": "muse-local", "quality": "coding", "balanced": "coding"}
    assert preview["chosen"] == expected[strategy]


def test_preview_can_try_a_strategy_without_saving_it(scored, client):
    """ผู้ดูแลควรเห็นก่อนว่ากลยุทธ์ใหม่จะเลือกอะไร แล้วค่อยกดใช้ — พรีวิวต้องไม่แก้ค่าที่ตั้งไว้"""
    headers = auth(client.admin_key)
    tried = client.get("/admin/auto/preview?strategy=quality", headers=headers).json()
    assert tried["strategy"] == "quality" and tried["chosen"] == "coding"
    assert tried["configured_strategy"] == "fastest"

    again = client.get("/admin/auto/preview", headers=headers).json()
    assert again["strategy"] == again["configured_strategy"] == "fastest"
    assert set(again["strategies"]) == {"fastest", "roomiest", "quality", "balanced"}

    assert client.get("/admin/auto/preview?strategy=smartest", headers=headers).status_code == 400


# ---------------------------------------------------------------------------
# 6. คอนโซล: วาดจากคำตอบจริงของ API ด้วยฟังก์ชันจริงของหน้าเว็บ (รันใน node)
# ---------------------------------------------------------------------------
def _render(preview: dict, *, admin: bool) -> str:
    import json
    import re
    import shutil
    import subprocess

    from tests.test_context_per_request import APP_JS, _js_function

    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const AUTO_STRATEGY_HELP = {")
    help_text = source[start:re.compile(r"^};\n", re.M).search(source, start).end()]
    script = f"""
{_js_function("esc")}
{help_text}
{_js_function("autoPreviewHtml")}
console.log(autoPreviewHtml({json.dumps(preview)}, {json.dumps(admin)}));
"""
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _cells(html: str, alias: str) -> list[str]:
    """ข้อความในแต่ละช่องของแถวโมเดลนั้น — อ่านจาก HTML ที่วาดออกมา ไม่ใช่จาก JSON ที่ส่งเข้าไป"""
    import re

    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S) if f">{alias}<" in r)
    return [" ".join(c.split()) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]


def test_console_shows_the_breakdown_the_ranker_used(scored, client):
    _traffic(client, muse_local=200.0, coding=40.0)
    assert _set_strategy(client, "balanced").status_code == 200
    preview = client.get("/admin/auto/preview?prompt_tokens=1000",
                         headers=auth(client.admin_key)).json()

    html = _render(preview, admin=True)

    # อันดับ · ชื่อ · คุณภาพ · tok/s · TTFT · ความเร็วเทียบตัวเร็วสุด · คะแนนรวม · context · จำนวนคำขอ
    assert _cells(html, "coding") == [
        "1", "coding", "90", "40.0", "200 ms", "20%", "66.7", "262,144", str(MIN_SAMPLES)]
    assert _cells(html, "muse-local")[2:7] == ["40", "200.0", "200 ms", "100%", "60.0"]
    # ยังไม่มีคะแนนและยังไม่มีสถิติ: ทุกช่องเป็นขีด ไม่ใช่ 0 — 0 คือคะแนนที่มีคนตั้ง
    assert _cells(html, "gemma-vision")[2:7] == ["—", "—", "—", "—", "—"]
    assert "<strong>coding</strong>" in html
    for name in ("fastest", "roomiest", "quality", "balanced"):
        assert f'<option value="{name}"' in html
    assert '<option value="balanced" selected>balanced (in use)</option>' in html


def test_console_marks_a_speed_that_was_assumed(scored, client):
    """ตัวที่ยังไม่มีสถิติถูกคิดเสมือนเร็วเท่าตัวเร็วสุด — หน้าจอต้องบอก ไม่ใช่โชว์ 100% เฉย ๆ"""
    _traffic(client, muse_local=200.0)                # coding มีคะแนนแต่ยังไม่มีสถิติ
    preview = client.get("/admin/auto/preview?strategy=balanced",
                         headers=auth(client.admin_key)).json()
    cells = _cells(_render(preview, admin=True), "coding")
    assert cells[3] == "—" and cells[5] == "100% *", cells


def test_console_offers_the_save_button_only_for_a_change_and_only_to_an_admin(scored, client):
    headers = auth(client.admin_key)
    current = client.get("/admin/auto/preview", headers=headers).json()
    trial = client.get("/admin/auto/preview?strategy=quality", headers=headers).json()

    assert 'id="auto-strategy-save" class="primary small" disabled' in _render(current, admin=True)
    as_admin = _render(trial, admin=True)
    assert 'id="auto-strategy-save" class="primary small">' in as_admin
    assert "ตัวอย่างเท่านั้น" in as_admin and "<strong>fastest</strong>" in as_admin
    # ผู้จัดการดูและลองได้ แต่ไม่มีปุ่ม — API ก็ปฏิเสธอยู่แล้ว (403) ปุ่มที่กดแล้วพังไม่ควรมี
    as_manager = _render(trial, admin=False)
    assert "auto-strategy-save" not in as_manager and 'id="auto-strategy"' in as_manager


def test_console_keeps_the_strategy_picker_when_nothing_can_serve(client):
    """ไม่มีผู้สมัครสักตัว (prompt ใหญ่เกินทุกโมเดล) ก็ยังต้องเปลี่ยนกลยุทธ์ได้"""
    preview = client.get("/admin/auto/preview?prompt_tokens=2000000",
                         headers=auth(client.admin_key)).json()
    assert preview["ranked"] == []
    html = _render(preview, admin=True)
    assert 'id="auto-strategy"' in html and 'class="empty"' in html
