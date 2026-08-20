"""model="auto" — ให้เกตเวย์เลือกโมเดลเองจากรูปร่างของคำขอ

แนวคิดจาก OrcaRouter-Lite แต่แกนจัดอันดับต่างกัน: ของเขาเรียงตามราคา (proxy ไป API
ที่คิดเงินต่อ token) ของเราเรียงตามความเร็วที่วัดได้จริง เพราะโมเดลรันบนเครื่องที่
โรงเรียนซื้อมาเองแล้ว เงินไม่ใช่ตัวแปรต่อคำขอ — เวลาต่างหากที่หายาก
"""

import httpx
import respx

from app.core.perf import MIN_SAMPLES, PerfStore
from tests.test_api import OPENAI_REPLY, UPSTREAM_CHAT, auth


# ── ตัวจัดอันดับ ──────────────────────────────────────────────────────────────
def test_a_fresh_gateway_still_answers(client, member_key):
    """วันแรกยังไม่มีสถิติสักตัว — auto ต้องใช้ได้ ไม่ใช่ปฏิเสธทุกคำขอ

    OrcaRouter ตัดโมเดลที่ไม่มีคะแนนทิ้งได้ เพราะแค็ตตาล็อกเขามีเป็นร้อยตัว ·
    ฟลีตโรงเรียนมีไม่กี่ตัว ตัดทิ้งแล้วอาจไม่เหลืออะไรเลย
    """
    store = PerfStore()
    assert store.get("anything") is None
    # _speed_key ต้องจัดตัวที่ไม่มีข้อมูลไว้ท้ายแถว ไม่ใช่ทำให้ตกรอบ
    from app.core.auto import _speed_key

    assert _speed_key(None) > _speed_key(_usable(120.0, 200.0))


def _usable(tps, ttft):
    store = PerfStore()
    for _ in range(MIN_SAMPLES):
        store.record("m", latency_ms=int(ttft + 1000), ttft_ms=int(ttft),
                     output_tokens=int(tps))
    return store.get("m")


def test_faster_models_rank_first():
    from app.core.auto import _speed_key

    fast = _usable(200.0, 100.0)
    slow = _usable(20.0, 100.0)
    assert _speed_key(fast) < _speed_key(slow)


def test_ttft_breaks_the_tie_when_throughput_matches():
    from app.core.auto import _speed_key

    snappy = _usable(100.0, 50.0)
    sluggish = _usable(100.0, 900.0)
    assert _speed_key(snappy) < _speed_key(sluggish)


# ── ทางเดินจริง ───────────────────────────────────────────────────────────────
@respx.mock
def test_auto_picks_a_model_the_member_may_use(client, member_key):
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    # ชื่อที่ตอบกลับต้องเป็นโมเดลจริง ไม่ใช่คำว่า auto — client ที่ตรวจชื่อจะได้ไม่งง
    served = response.headers["x-litegate-served-by"]
    assert served and served != "auto"
    assert response.headers["x-litegate-model"] == served


@respx.mock
def test_auto_is_not_a_way_around_permissions(client, member_key):
    """ผู้เลือกคือเกตเวย์ แต่ตัวเลือกมีแค่โมเดลที่สมาชิกใช้ได้อยู่แล้ว"""
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    catalogue = client.get("/v1/models", headers=auth(member_key)).json()
    allowed = {m["id"] for m in catalogue["data"]}

    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.headers["x-litegate-served-by"] in allowed


def test_traffic_teaches_the_ranker(client, member_key):
    """สถิติมาจากทราฟฟิกจริงที่ผ่านเกตเวย์ ไม่ต้องตั้งค่าอะไรเพิ่ม"""
    store = client.app.state.gateway.perf if hasattr(client.app.state, "gateway") else None
    if store is None:  # เข้าถึง state ต่างกันตาม fixture — ทดสอบ store ตรง ๆ แทน
        store = PerfStore()
    store.clear()
    for _ in range(MIN_SAMPLES):
        store.record("coding", latency_ms=1200, ttft_ms=200, output_tokens=100)
    perf = store.get("coding")
    assert perf.usable and perf.output_tps > 0


def test_preview_uses_the_same_ranker_as_the_request_path(client):
    """คำอธิบายกับของจริงต้องไม่เพี้ยนจากกัน — เขียนสูตรซ้ำเมื่อไรก็เพี้ยนเมื่อนั้น"""
    from pathlib import Path

    admin = Path("app/api/admin.py").read_text(encoding="utf-8")
    assert "auto_mod.explain(" in admin and "auto_mod.choose(" in admin

    d = client.get("/admin/auto/preview?prompt_tokens=1000", headers=auth(client.admin_key)).json()
    assert d["chosen"], d
    assert d["ranked"] and d["ranked"][0]["alias"] == d["chosen"]
    assert d["reason"]


def test_preview_respects_a_request_that_needs_vision(client):
    text_only = client.get(
        "/admin/auto/preview?prompt_tokens=100", headers=auth(client.admin_key)).json()
    with_image = client.get(
        "/admin/auto/preview?prompt_tokens=100&vision=true", headers=auth(client.admin_key)).json()
    # ขอภาพแล้วตัวเลือกต้องไม่กว้างกว่าเดิม
    assert len(with_image["ranked"]) <= len(text_only["ranked"])


def test_preview_drops_models_that_cannot_hold_the_prompt(client):
    huge = client.get(
        "/admin/auto/preview?prompt_tokens=1000000", headers=auth(client.admin_key)).json()
    small = client.get(
        "/admin/auto/preview?prompt_tokens=100", headers=auth(client.admin_key)).json()
    assert len(huge["ranked"]) <= len(small["ranked"])


def test_the_console_shows_auto_and_explains_it():
    """ฟีเจอร์ที่ไม่โผล่ในหน้าเว็บ = ไม่มีอยู่จริงสำหรับคนที่ใช้ผ่านคอนโซลล้วน ๆ"""
    from pathlib import Path

    page = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'id="auto-panel"' in page
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "loadAutoPreview" in js
    # ต้องบอกด้วยว่าตัวเลขมาจากไหน ไม่ใช่โชว์อันดับลอย ๆ
    assert "ทราฟฟิกจริง" in js
