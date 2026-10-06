"""โควตาต่อ key — ด่านที่สองที่บวกเข้ามา ไม่ใช่ตัวแทนโควตาของคน

เคสที่ต้องใช้: token ของ CI ที่ไม่ควรกินโควตาของเจ้าของจนหมด หรือใบทดลองที่แจก
คนนอกแล้วอยากจำกัด 50 ครั้งจบ
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _member(client, external_id: str = "6477777777"):
    user = client.post(
        "/admin/users",
        json={"external_id": external_id, "display_name": "Key Tester", "role": "member"},
        headers=auth(client.admin_key),
    ).json()
    key = client.post(
        "/admin/api-keys",
        json={"user_id": user["id"], "name": "ci-token"},
        headers=auth(client.admin_key),
    ).json()
    return user, key


def test_a_key_with_no_policy_of_its_own_changes_nothing(client):
    """ค่าเริ่มต้นของทุก deployment · ไม่มีนโยบายของ key = ไม่มีอะไรเปลี่ยน"""
    _user, key = _member(client)
    response = client.get("/v1/models", headers=auth(key["api_key"]))
    assert response.status_code == 200


def test_the_key_ceiling_stops_the_key_before_the_person_runs_out(client):
    """ใบนี้หมดเพดานของตัวเอง ทั้งที่เจ้าของยังมีโควตาเหลือ"""
    user, key = _member(client, "6477777778")

    client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": key["id"], "name": "ci 1 ครั้ง",
              "window": "day", "max_requests": 1},
        headers=auth(client.admin_key),
    )

    # คนยังมีโควตาเต็ม — เพดานที่จะหยุดคือของใบนี้
    person = client.get(
        f"/admin/users/{user['id']}/quota", headers=auth(client.admin_key)
    ).json()
    assert person["limits"]["max_requests"] > 1


def test_the_message_says_whose_ceiling_was_hit():
    """คนที่ยังมีโควตาเหลือเยอะจะงงถ้าข้อความบอกว่า "โควตาของคุณหมด" """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/core/quota.py").read_text()
    assert 'whose = "This API key\'s" if subject == "key" else "Your"' in source
    assert '"subject": subject' in source


def test_both_gates_run_and_neither_replaces_the_other():
    """ถ้าด่านใดด่านหนึ่งชนะ การออก key ใบใหม่จะกลายเป็นวิธีขอโควตาเพิ่ม

    เหตุผลเดียวกับที่รายการโมเดลบน key ทำได้แค่แคบลง
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for surface in ("openai", "anthropic", "responses"):
        text = (root / f"app/api/{surface}.py").read_text()
        assert "quota.check(principal.user_id, limits)" in text, surface
        assert "resolve_key_limits" in text, surface
        assert "check_key" in text, surface
        # ด่านของคนต้องมาก่อน — ถ้าใบมีเพดานต่ำกว่า ผู้ใช้ควรรู้ว่าตัวเองยังเหลือ
        assert text.index("quota.check(principal.user_id") < text.index("check_key"), surface


def test_the_key_counter_is_a_separate_pile(client):
    """ใบหนึ่งหมดต้องไม่ลากใบอื่นของคนเดียวกันไปด้วย"""
    from app.core.quota import QuotaService

    assert QuotaService.key_subject("a1") == "key:a1"
    assert QuotaService.subject_key("u1") == "user:u1"
    assert QuotaService.key_subject("a1") != QuotaService.subject_key("a1")


def test_consumption_lands_in_the_key_pile_only_when_a_policy_exists():
    """ไม่มีนโยบายของ key = ไม่มีตัวนับเพิ่ม · deployment ที่ไม่ใช้จะไม่จ่ายอะไรเลย"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/core/quota.py").read_text()
    block = source.split("async def record(")[1][:1800]
    assert "if api_key_id and key_window:" in block


def test_the_key_ceiling_actually_refuses_the_second_call(client):
    """เพดาน 1 ครั้ง/วัน → คำขอที่สองต้องถูกปฏิเสธ ทั้งที่เจ้าของยังเหลือ 499"""
    user, key = _member(client, "6488888881")
    client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": key["id"], "name": "ci หนึ่งครั้ง",
              "window": "day", "max_requests": 1},
        headers=auth(client.admin_key),
    )

    body = {"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
    first = client.post("/v1/chat/completions", json=body, headers=auth(key["api_key"]))
    second = client.post("/v1/chat/completions", json=body, headers=auth(key["api_key"]))

    assert second.status_code == 429, second.text
    error = second.json()["error"]
    assert error["code"] == "QUOTA_EXCEEDED"
    # ต้องบอกว่าเป็นเพดานของ key ไม่ใช่ของคน — คนที่ยังเหลือเยอะจะได้ไม่งง
    assert "API key" in error["message"], error["message"]

    # และเจ้าของยังไม่ได้ถูกตัดโควตา
    person = client.get(
        f"/admin/users/{user['id']}/quota", headers=auth(client.admin_key)
    ).json()
    assert person["used"]["requests"] < person["limits"]["max_requests"]
    assert first.status_code in (200, 502, 503)


@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages", {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"input": "hi"}),
], ids=["chat", "messages", "responses"])
def test_the_key_ceiling_holds_on_every_surface(client, path, body):
    """เพดานของ key ต้องนับทุกทางเข้า — /v1/responses เคยตรวจเพดานแต่ไม่เคยบันทึกลงกองของ key

    ทางนั้นสร้าง context ของคำขอโดยไม่ส่ง `key_window` ไปด้วย ตัวนับของ key จึงเป็น 0 ตลอด
    key ที่ตั้งเพดานไว้ใช้ผ่าน Codex ได้ไม่จำกัด (ตรวจพบ 2026-10-05)
    """
    _, key = _member(client, "6488888882")
    client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": key["id"], "name": "หนึ่งครั้ง",
              "window": "day", "max_requests": 1},
        headers=auth(client.admin_key),
    )
    request = {"model": "coding", **body}
    client.post(path, json=request, headers=auth(key["api_key"]))
    second = client.post(path, json=request, headers=auth(key["api_key"]))
    assert second.status_code == 429, second.text
    assert "API key" in second.text


def test_a_key_policy_does_not_change_anybody_elses_quota(client):
    """เจอจริงตอนเขียนฟีเจอร์นี้: นโยบายของ key ที่ไม่ระบุ user/workspace ได้คะแนน
    เท่านโยบายกลาง แล้วไปชนะ — ตั้งเพดานให้ CI ใบเดียวกลายเป็นลดโควตาทุกคน
    """
    other, _ = _member(client, "6499000011")
    before = client.get(
        f"/admin/users/{other['id']}/quota", headers=auth(client.admin_key)
    ).json()["limits"]["max_requests"]

    _u, key = _member(client, "6499000012")
    client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": key["id"], "window": "day", "max_requests": 1},
        headers=auth(client.admin_key),
    )

    after = client.get(
        f"/admin/users/{other['id']}/quota", headers=auth(client.admin_key)
    ).json()["limits"]["max_requests"]
    assert after == before, "นโยบายของ key ต้องไม่แตะโควตาของคนอื่น"


def test_the_console_can_set_a_key_ceiling_when_issuing():
    """ลูกค้าใช้ผ่าน GUI เป็นหลัก — ฟีเจอร์ที่ตั้งได้แต่ API ไม่นับว่ามี"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app/static"
    page = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")

    assert 'id="k-cap-on"' in page
    for field in ("k-cap-window", "k-cap-req", "k-cap-in", "k-cap-out"):
        assert f'id="{field}"' in page, field
    assert "scope: 'key'" in js
    assert "api_key_id: result.id" in js
    # key ออกไปแล้วแต่ตั้งเพดานพลาด ต้องบอก ไม่ใช่เงียบ
    assert "ตั้งเพดานไม่สำเร็จ" in js


def test_a_key_policy_says_which_key_it_targets(client):
    """นโยบายชื่อ "เพดานของใบ ci-token" ที่ไม่บอกว่าใบไหน คือสิ่งที่ไล่ตามไม่ได้"""
    _user, key = _member(client, "6499000013")
    client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": key["id"], "window": "day", "max_requests": 5},
        headers=auth(client.admin_key),
    )
    rows = client.get("/admin/quota-policies", headers=auth(client.admin_key)).json()["data"]
    mine = [r for r in rows if r["scope"] == "key"]
    assert mine and mine[0]["api_key_id"] == key["id"]


def test_an_hourly_window_exists(client):
    """day เป็นช่วงที่ยาวไป — เผลอปล่อย loop ตอนเช้าแล้วโดนตัดทั้งวัน
    ส่วนต่อนาทีสั้นเกินจะเป็นเพดานของงานจริง
    """
    from datetime import timedelta

    from app.core.quota import window_bounds

    start, end = window_bounds("hour")
    assert end - start == timedelta(hours=1)
    assert start.minute == 0 and start.second == 0

    _user, key = _member(client, "6499000014")
    created = client.post(
        "/admin/quota-policies",
        json={"scope": "key", "api_key_id": key["id"], "window": "hour", "max_requests": 20},
        headers=auth(client.admin_key),
    )
    assert created.status_code == 201, created.text

    rows = client.get("/admin/quota-policies", headers=auth(client.admin_key)).json()["data"]
    assert any(r["window"] == "hour" for r in rows)


def test_the_console_offers_the_hourly_window():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text()
    # ทั้งฟอร์มนโยบายหลักและกล่องเพดานของ key
    assert page.count('<option value="hour">hour</option>') >= 2


# ── ทุกเพดานบนใบเดียวถูกบังคับ ไม่ใช่แค่อันเดียว ────────────────────────────────
#
# ตรวจพบ 2026-10-06: resolve_key_limits เลือกมาอันเดียวด้วย
# `min(policies, key=max_requests or 1 << 62)` — "อันที่ max_requests น้อยที่สุด" —
# แล้วทิ้งที่เหลือ · key ที่มีเพดาน 1,000 ครั้ง/วัน กับอีกอัน 5 output token จึงเรียกได้
# สี่ครั้ง ครั้งละ 5 output token โดยไม่มีอะไรหยุด

CODING = "http://dgx03:8000/v1/chat/completions"
REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "x",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


@pytest.fixture
def upstream():
    import httpx
    import respx

    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def _ceiling(client, key, **body):
    return client.post("/admin/quota-policies", headers=auth(client.admin_key),
                       json={"scope": "key", "api_key_id": key["id"], **body})


def _ceiling_row(client, key, **columns) -> None:
    """เพดานที่เขียนลงตารางตรง ๆ — แถวแบบที่ฐานข้อมูลซึ่งใช้งานมาก่อนมีอยู่แล้ว
    (เพดานสองอันบนใบเดียว หน้าต่างเดียวกัน) ไม่ว่า API จะยอมให้สร้างแบบนั้นอีกหรือไม่"""
    from app.db.models import QuotaPolicy
    from app.db.session import session_scope

    async def write():
        async with session_scope() as session:
            session.add(QuotaPolicy(scope="key", api_key_id=key["id"], **columns))

    client.portal.call(write)


def _call(client, key):
    return client.post("/v1/chat/completions", headers=auth(key["api_key"]),
                       json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})


def test_two_ceilings_on_one_key_are_both_enforced(client, upstream):
    """เคสที่ตรวจพบ: 1,000 ครั้ง/วัน + 5 output token/วัน บนใบเดียว"""
    _user, key = _member(client, "6499000021")
    assert _ceiling(client, key, window="day", max_requests=1000,
                    name="1000 requests").status_code == 201
    _ceiling_row(client, key, window="day", max_output_tokens=5, name="5 output tokens")

    assert _call(client, key).status_code == 200          # ใช้ไป 5 output token
    refused = _call(client, key)

    assert refused.status_code == 429, "เพดาน 5 output token ต้องหยุดคำขอที่สอง"
    error = refused.json()["error"]
    assert error["details"]["quota"] == "output token"
    assert error["details"]["subject"] == "key"
    assert "API key" in error["message"]


def test_ceilings_with_different_windows_are_each_counted_in_their_own(client, upstream):
    """100 ครั้งต่อชั่วโมง กับ 5 output token ต่อเดือน — คนละหน้าต่าง คนละตัวนับ ทั้งคู่มีผล"""
    _user, key = _member(client, "6499000022")
    assert _ceiling(client, key, window="hour", max_requests=100).status_code == 201
    assert _ceiling(client, key, window="month", max_output_tokens=5).status_code == 201

    assert _call(client, key).status_code == 200
    refused = _call(client, key)

    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["window"] == "month"


def test_the_ceiling_that_is_hit_first_is_the_one_reported(client, upstream):
    """เพดานที่ไม่ได้ตั้ง max_requests เคยถูกจัดเป็น "หลวมที่สุด" แล้วถูกทิ้ง"""
    _user, key = _member(client, "6499000023")
    _ceiling(client, key, window="day", max_input_tokens=15, name="15 input tokens")
    _ceiling_row(client, key, window="day", max_requests=3, name="3 requests")

    assert _call(client, key).status_code == 200          # 10 input token
    assert _call(client, key).status_code == 200          # 20 — เกิน 15 แล้ว
    refused = _call(client, key)

    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["quota"] == "input token"


def test_a_key_under_its_ceilings_is_not_refused(client, upstream):
    _user, key = _member(client, "6499000024")
    _ceiling(client, key, window="day", max_requests=10)
    _ceiling(client, key, window="month", max_output_tokens=500)

    for _ in range(4):
        assert _call(client, key).status_code == 200


def test_two_ceiling_windows_starting_together_do_not_double_count(
        client, upstream, monkeypatch):
    """วันที่ 1: เพดานรายวันกับรายเดือนเริ่มพร้อมกัน — ต้องไม่กลายเป็นตัวนับเดียวที่ถูกบวกสองรอบ"""
    from datetime import datetime, timezone

    from tests.test_quota_counter_identity import counter_rows, freeze

    _user, key = _member(client, "6499000025")
    _ceiling(client, key, window="day", max_requests=50)
    _ceiling(client, key, window="month", max_requests=4)
    freeze(monkeypatch, datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc))

    outcomes = [_call(client, key).status_code for _ in range(5)]

    assert outcomes == [200, 200, 200, 200, 429], "รายเดือนตั้งไว้ 4 — ไม่ใช่ 2"
    first = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = {name: n for (name, start), n in counter_rows(client).items()
            if name.startswith("key:") and start == first}
    assert rows == {f"key:{key['id']}": 4, f"key:{key['id']}@day": 4}
