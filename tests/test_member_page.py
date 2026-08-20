"""หน้า self-service ของสมาชิก — เอา key ของตัวเองมากรอกแล้วดูสิทธิ์/โควตา/การใช้งาน

ตั้งใจให้ไม่เปิดเผยอะไรที่คนถือ key ไม่ได้มีอยู่แล้ว · ตัว key เองเขามี ส่วนขอบเขตของมัน
เขาชนเข้าอยู่ทุกวันเวลาถูกปฏิเสธ — การบอกตรง ๆ เปลี่ยนการเดาให้เป็นข้อมูล
"""

import httpx
import respx

from tests.test_api import OPENAI_REPLY, UPSTREAM_CHAT, auth


def test_a_member_sees_the_facts_about_their_own_key(client, member_key):
    d = client.get("/v1/me/key", headers=auth(member_key)).json()
    assert d["via"] == "key"
    key = d["key"]
    assert key["prefix"] and key["prefix"] in member_key, "prefix ต้องช่วยเทียบได้ว่าใบไหน"
    for field in ("label", "issued_at", "expires_at", "last_used_at",
                  "limited_to_models", "limited_to_groups"):
        assert field in key, field


def test_the_key_itself_is_never_returned(client, member_key):
    """หน้านี้มีไว้ให้ดูขอบเขต ไม่ใช่ให้ดึง key กลับมา — hash ก็ไม่ควรหลุดออกไป"""
    body = client.get("/v1/me/key", headers=auth(member_key)).text
    assert member_key not in body
    for leak in ("key_hash", "key_sealed", "hash"):
        assert leak not in body, leak


@respx.mock
def test_usage_shows_where_the_quota_went(client, member_key):
    """โควตาบอกแค่ "เหลือเท่าไร" — ตอบไม่ได้ว่าหมดไปกับอะไร"""
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    d = client.get("/v1/me/usage?days=7", headers=auth(member_key)).json()
    assert d["window_days"] == 7
    assert d["daily"] and d["daily"][-1]["requests"] >= 1
    coding = next(r for r in d["by_model"] if r["model"] == "coding")
    assert coding["output_tokens"] > 0


@respx.mock
def test_usage_is_only_ever_your_own(client, member_key):
    """ไม่มีพารามิเตอร์ให้ระบุคนอื่น และ admin ที่เรียกก็เห็นแค่ของตัวเอง"""
    respx.post(UPSTREAM_CHAT).mock(return_value=httpx.Response(200, json=OPENAI_REPLY))
    client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    admin_view = client.get("/v1/me/usage?days=7", headers=auth(client.admin_key)).json()
    member_view = client.get("/v1/me/usage?days=7", headers=auth(member_key)).json()
    admin_requests = sum(d["requests"] for d in admin_view["daily"])
    member_requests = sum(d["requests"] for d in member_view["daily"])
    assert member_requests >= 1
    assert admin_requests != member_requests or admin_requests == 0


def test_a_bad_key_is_refused(client):
    assert client.get("/v1/me/key", headers=auth("lg_sk_nope")).status_code == 401
    assert client.get("/v1/me/usage", headers=auth("lg_sk_nope")).status_code == 401


def test_the_page_checks_the_key_shape_before_sending_it():
    """header HTTP รับได้แค่ latin-1 — key ที่มีอักษรไทย/อีโมจิติดมาจากการก๊อป
    จะทำให้ fetch โยน TypeError ซึ่งอ่านแล้วไม่รู้เรื่องเลยว่าเกิดอะไรขึ้น
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app/static/member/member.js").read_text()
    assert "looksLikeKey" in js


def test_only_models_the_member_can_call_are_listed():
    """หน้านี้ตอบว่า "ตอนนี้ฉันใช้อะไรได้" — โชว์ของที่กดแล้วโดนปฏิเสธคือชวนให้ลอง"""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app/static/member/member.js").read_text()
    assert "byAlias.values()" in js
    # ห้ามกลับไปเติมแถวของโมเดลที่ไม่มีสิทธิ์
    assert "เรียกไม่ได้แล้ว" not in js
    assert "reachable" not in js
