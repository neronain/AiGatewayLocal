"""Seeing who has used how much, without inventing a number.

The console could issue keys and set limits and then had nothing to say about
what happened next. The obvious thing to copy is a spend bar on each key row —
but the counters here are keyed by *user* and the limits resolve per user, so a
bar drawn on a key would put somebody else's figure under that key's name. It
would look authoritative and be wrong, which is worse than an empty column.

So the allowance is reported per person, and the key row reports what that key
actually did, which is a different and genuinely useful question: which of these
is still in use.
"""

from __future__ import annotations

import httpx
import pytest
import respx

UPSTREAM = "http://dgx03:8000/v1/chat/completions"

REPLY = {
    "id": "c1", "object": "chat.completion", "model": "m",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


@pytest.fixture
def upstream():
    with respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def ask(client, key):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})


def quota_rows(client):
    return client.get("/admin/usage/quota", headers=auth(client.admin_key)).json()["data"]


def key_rows(client):
    return client.get("/admin/usage/by-key?days=7",
                      headers=auth(client.admin_key)).json()["data"]


def test_usage_shows_up_against_the_persons_allowance(client, member_key, upstream):
    client.post("/admin/quota-policies", headers=auth(client.admin_key),
                json={"scope": "global", "window": "day", "max_requests": 10})
    ask(client, member_key)
    ask(client, member_key)

    row = next(r for r in quota_rows(client) if r["used"]["requests"] == 2)
    assert row["limits"]["max_requests"] == 10
    assert row["percent"] == 20


def test_the_percentage_follows_the_tightest_limit(client, member_key, upstream):
    """สี่ลิมิตพร้อมกันได้ · ตัวที่จะหยุดเขาจริงคือตัวที่ใกล้เต็มที่สุด

    ถ้าวาดตัวที่เหลือเยอะสุด บาร์จะอ่านว่า "สบาย" จนถึงวินาทีที่โดนปฏิเสธ
    """
    client.post("/admin/quota-policies", headers=auth(client.admin_key),
                json={"scope": "global", "window": "day",
                      "max_requests": 1000, "max_output_tokens": 10})
    ask(client, member_key)  # 5 output tokens ของ 10 = 50%

    row = next(r for r in quota_rows(client) if r["used"]["requests"])
    assert row["percent"] == 50, "ต้องรายงานตัวที่ตึงที่สุด ไม่ใช่ตัวที่หลวมที่สุด"


def test_no_limit_reports_no_percentage_rather_than_zero(client, member_key, upstream):
    """0% อ่านว่า "ยังไม่ได้ใช้" · ไม่จำกัดต้องไม่ถูกวาดเป็นบาร์ว่าง

    ต้องตั้งนโยบายที่ทุกช่องเป็น 0 · ไม่ตั้งอะไรเลยไม่ได้แปลว่าไม่จำกัด — ค่าตั้งต้น
    ใน gateway.yaml ก็เป็นลิมิตเหมือนกัน
    """
    client.post("/admin/quota-policies", headers=auth(client.admin_key),
                json={"scope": "global", "window": "day", "max_requests": 0,
                      "max_input_tokens": 0, "max_output_tokens": 0, "max_images": 0})
    ask(client, member_key)
    row = next(r for r in quota_rows(client) if r["used"]["requests"])
    assert row["percent"] is None


def test_it_says_which_rule_the_limit_came_from(client, member_key, upstream):
    client.post("/admin/quota-policies", headers=auth(client.admin_key),
                json={"scope": "global", "window": "day", "max_requests": 10})
    ask(client, member_key)
    assert next(r for r in quota_rows(client) if r["used"]["requests"])["source"] == "global"


def test_activity_is_reported_for_the_key_that_did_it(client, member_key, upstream):
    ask(client, member_key)
    ask(client, member_key)

    rows = key_rows(client)
    assert rows, "ต้องมีกิจกรรมของ key ที่เพิ่งยิงไป"
    assert sum(r["requests"] for r in rows) == 2
    assert sum(r["tokens"] for r in rows) == 30


def test_two_keys_of_one_person_are_counted_apart(client, member_key, upstream):
    """คำถามที่รายการ key ตอบไม่ได้มาก่อน: ใบไหนยังใช้อยู่ ใบไหนออกไว้แล้วลืม"""
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    me = next(u for u in users if u["role"] == "member")
    second = client.post("/admin/api-keys", headers=auth(client.admin_key),
                         json={"user_id": me["id"], "name": "other"}).json()["api_key"]

    ask(client, member_key)
    ask(client, second)
    ask(client, second)

    counts = sorted(r["requests"] for r in key_rows(client))
    assert counts == [1, 2], "แยกตามใบ ไม่ใช่รวมของเจ้าของ"


def test_a_member_cannot_read_the_usage_of_others(client, member_key):
    assert client.get("/admin/usage/quota", headers=auth(member_key)).status_code in (401, 403)
    assert client.get("/admin/usage/by-key", headers=auth(member_key)).status_code in (401, 403)


def test_a_manager_sees_only_their_own_class(client, upstream):
    lecturer = client.post("/admin/users", headers=auth(client.admin_key),
                           json={"external_id": "lecturer", "role": "manager"}).json()
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "CS101", "name": "CS101"}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": ["coding"]})
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": lecturer["id"]})
    their_key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                            json={"user_id": lecturer["id"], "name": "k"}).json()["api_key"]

    seen = client.get("/admin/usage/quota", headers=auth(their_key)).json()["data"]
    assert {r["external_id"] for r in seen} == {"lecturer"}
