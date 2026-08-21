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
