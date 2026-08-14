"""Stopping a burst, which a daily quota cannot do.

A day's quota keeps somebody from spending a term's worth in a week. It does
nothing about forty people in a classroom pressing send within the same minute,
which is the shape of the load this actually gets: the machines queue, and the
person who asked last waits minutes for a first token while their own daily
figure is barely touched.

Counted per person rather than per key, because opening ten tabs is still one
person — and because counting per key would mean issuing yourself another key
was a way to get more.
"""

from __future__ import annotations

import httpx
import pytest
import respx

UPSTREAM = "http://dgx03:8000/v1/chat/completions"

REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "whatever",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def policy(client, **limits):
    return client.post("/admin/quota-policies", headers=auth(client.admin_key),
                       json={"scope": "global", "window": "day", **limits})


def ask(client, key):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})


@pytest.fixture
def upstream():
    with respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def test_a_burst_is_refused_once_the_minute_is_spent(client, member_key, upstream):
    policy(client, max_requests_per_minute=2)

    assert ask(client, member_key).status_code == 200
    assert ask(client, member_key).status_code == 200

    third = ask(client, member_key)
    assert third.status_code == 429
    body = third.json()["error"]
    assert body["details"]["window"] == "minute"
    assert body["details"]["limit"] == 2


def test_the_refusal_says_how_long_to_wait(client, member_key, upstream):
    """"กลับมาพรุ่งนี้" กับ "รออีก 40 วินาที" คือคนละคำตอบ"""
    policy(client, max_requests_per_minute=1)
    ask(client, member_key)

    refused = ask(client, member_key)
    assert refused.status_code == 429
    wait = int(refused.headers["retry-after"])
    assert 1 <= wait <= 60, "ต้องเป็นวินาทีที่รอจริง ไม่ใช่ข้ามวัน"
    assert "second" in refused.json()["error"]["message"]


def test_a_token_burst_is_refused_too(client, member_key, upstream):
    policy(client, max_tokens_per_minute=20)

    assert ask(client, member_key).status_code == 200  # 15 โทเคน
    assert ask(client, member_key).status_code == 200  # รวม 30 เกินแล้ว

    refused = ask(client, member_key)
    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["quota"] == "token"


def test_without_a_rate_limit_nothing_changes(client, member_key, upstream):
    """ค่าตั้งต้นคือไม่จำกัด · ลิมิตที่ไม่มีใครเลือกจะไปปฏิเสธคนกลางคาบโดยไม่มีใครอธิบายได้"""
    policy(client, max_requests=100)
    for _ in range(6):
        assert ask(client, member_key).status_code == 200


def test_the_burst_check_comes_before_the_daily_one(client, member_key, upstream):
    """เกินทั้งคู่ได้พร้อมกัน · บอกให้รอ 40 วินาทีมีประโยชน์กว่าบอกให้กลับมาพรุ่งนี้"""
    policy(client, max_requests=1, max_requests_per_minute=1)
    ask(client, member_key)

    refused = ask(client, member_key)
    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["window"] == "minute"


def test_it_is_counted_per_person_not_per_key(client, member_key, upstream):
    """เปิดสิบแท็บก็ยังเป็นคนเดียว · ออก key เพิ่มต้องไม่ได้โควตาเพิ่มฟรี"""
    policy(client, max_requests_per_minute=2)

    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    me = next(u for u in users if u["role"] == "member")
    second = client.post("/admin/api-keys", headers=auth(client.admin_key),
                         json={"user_id": me["id"], "name": "another tab"}).json()["api_key"]

    assert ask(client, member_key).status_code == 200
    assert ask(client, second).status_code == 200
    assert ask(client, second).status_code == 429, "key ที่สองต้องไม่รีเซ็ตตัวนับ"


def test_two_people_do_not_share_a_budget(client, member_key, upstream):
    policy(client, max_requests_per_minute=1)
    other = client.post("/admin/users", headers=auth(client.admin_key),
                        json={"external_id": "someone-else"}).json()
    their_key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                            json={"user_id": other["id"], "name": "k"}).json()["api_key"]

    assert ask(client, member_key).status_code == 200
    assert ask(client, member_key).status_code == 429
    assert ask(client, their_key).status_code == 200, "คนละคนต้องไม่กินโควตากัน"


def test_the_window_is_a_real_minute():
    from datetime import UTC, datetime

    from app.core.quota import window_bounds

    now = datetime(2026, 8, 14, 10, 30, 45, tzinfo=UTC)
    start, end = window_bounds("minute", now)
    assert start == datetime(2026, 8, 14, 10, 30, tzinfo=UTC)
    assert (end - start).total_seconds() == 60


def test_the_policy_carries_the_new_limits_back(client):
    policy(client, max_requests_per_minute=30, max_tokens_per_minute=9000)
    listed = client.get("/admin/quota-policies", headers=auth(client.admin_key)).json()["data"]
    live = next(p for p in listed if p["max_requests_per_minute"] == 30)
    assert live["max_tokens_per_minute"] == 9000
