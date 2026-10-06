"""สร้างนโยบาย (POST) กับแก้นโยบาย (PATCH) ต้องตรวจด้วยตัวตรวจเดียวกัน

ตรวจพบ 2026-10-06:

* `POST /admin/quota-policies {"max_requests": -1}` → 201 แล้วทุกคนได้
  `429 Your day request quota is exhausted (0 of -1)` ตั้งแต่คำขอแรกของวัน ·
  PATCH ค่าเดียวกัน → `400 max_requests cannot be negative.`
* `POST {"scope": "user"}` ที่ไม่มี `user_id` → 201 แล้วนโยบาย "ของคนเดียว" ใช้กับทุกคน

ค่าเดียวกันต้องหมายความอย่างเดียวกันไม่ว่าจะเข้ามาทางไหน — ทีมเคยเจ็บกับ "ว่าง =
ไม่จำกัด" บนรายการโมเดลของ key มาแล้ว
"""

from __future__ import annotations

import httpx
import pytest
import respx

CODING = "http://dgx03:8000/v1/chat/completions"
REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "x",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
LIMITS = ["max_requests", "max_input_tokens", "max_output_tokens", "max_images",
          "max_requests_per_minute", "max_tokens_per_minute"]
INT64_MAX = 2**63 - 1


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


@pytest.fixture
def upstream():
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def create(client, **body):
    return client.post("/admin/quota-policies", headers=auth(client.admin_key), json=body)


def edit(client, policy_id, **body):
    return client.patch(f"/admin/quota-policies/{policy_id}",
                        headers=auth(client.admin_key), json=body)


def listed(client, policy_id) -> dict:
    rows = client.get("/admin/quota-policies", headers=auth(client.admin_key)).json()["data"]
    return next(r for r in rows if r["id"] == policy_id)


def ask(client, key):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})


def message(response) -> str:
    return response.json()["error"]["message"]


def someone(client, external_id="someone-else"):
    user = client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id}).json()
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": user["id"], "name": "k"}).json()
    return user, key


# ---------------------------------------------------------------------------
# ลิมิต: ค่าเดียวกัน คำตอบเดียวกัน ทั้งสองทาง
# ---------------------------------------------------------------------------
REFUSED = [
    (-1, "negative"),
    (None, "null"),
    ("5", "whole number"),
    (True, "whole number"),
    (1.5, "whole number"),
    ([5], "whole number"),
    (INT64_MAX + 1, "too large"),
]


@pytest.mark.parametrize("field", LIMITS)
@pytest.mark.parametrize("value, why", REFUSED, ids=[repr(v) for v, _ in REFUSED])
def test_a_value_refused_on_edit_is_refused_on_create_with_the_same_words(
        client, field, value, why):
    existing = create(client, max_requests=10).json()

    made = create(client, scope="global", window="month", **{field: value})
    patched = edit(client, existing["id"], **{field: value})

    assert made.status_code == 400, made.text
    assert patched.status_code == 400, patched.text
    assert message(made) == message(patched)
    assert why in message(made) and field in message(made)
    assert made.json()["error"]["param"] == field


ACCEPTED = [(0, 0), (1, 1), (5.0, 5), (5_000_000_000, 5_000_000_000), (INT64_MAX, INT64_MAX)]


@pytest.mark.parametrize("field", LIMITS)
@pytest.mark.parametrize("value, stored", ACCEPTED, ids=[repr(v) for v, _ in ACCEPTED])
def test_a_value_accepted_is_stored_the_same_either_way(client, field, value, stored):
    made = create(client, **{field: value})
    assert made.status_code == 201, made.text
    assert listed(client, made.json()["id"])[field] == stored

    other = create(client, window="month").json()
    assert edit(client, other["id"], **{field: value}).status_code == 200
    assert listed(client, other["id"])[field] == stored


def test_the_refused_negative_limit_never_reaches_a_member(client, member_key, upstream):
    """เคสที่ตรวจพบ: -1 ถูกรับไว้ แล้วคำขอแรกของวันของทุกคนได้ "(0 of -1)" """
    assert create(client, scope="global", window="day", max_requests=-1).status_code == 400
    assert client.get("/admin/quota-policies",
                      headers=auth(client.admin_key)).json()["data"] == []
    assert ask(client, member_key).status_code == 200


def test_zero_means_unlimited_and_absent_means_zero_on_create(client, member_key, upstream):
    made = create(client, scope="global", window="day", max_requests=0, max_input_tokens=25)
    assert made.status_code == 201
    row = listed(client, made.json()["id"])
    assert row["max_requests"] == 0 and row["max_output_tokens"] == 0, "ไม่ส่งมา = 0"

    # 0 ครั้ง = ไม่จำกัดจำนวนครั้ง: ผ่านจนกว่าเพดาน token (25) จะหยุด — ครั้งละ 10 token เข้า
    assert [ask(client, member_key).status_code for _ in range(4)] == [200, 200, 200, 429]


def test_absent_on_edit_leaves_the_stored_value_alone(client):
    made = create(client, max_requests=10, max_output_tokens=500).json()
    assert edit(client, made["id"], max_requests=20).status_code == 200
    row = listed(client, made["id"])
    assert (row["max_requests"], row["max_output_tokens"]) == (20, 500)


# ---------------------------------------------------------------------------
# หน้าต่าง · วันหมดอายุ · ชื่อ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("window", ["week", "minute", "", None, 5, "Day"])
def test_an_unknown_window_is_refused_either_way(client, window):
    existing = create(client, max_requests=10).json()

    made = create(client, window=window, max_requests=1)
    patched = edit(client, existing["id"], window=window)

    assert (made.status_code, patched.status_code) == (400, 400)
    assert message(made) == message(patched) == "window must be hour, day, month or term."


@pytest.mark.parametrize("window", ["hour", "day", "month", "term"])
def test_every_real_window_is_accepted_either_way(client, window):
    made = create(client, window=window, max_requests=1)
    assert made.status_code == 201
    other = create(client, model_alias="coding", max_requests=1).json()
    assert edit(client, other["id"], window=window).status_code == 200


@pytest.mark.parametrize("days", [0, -3, 1.5, "7", True])
def test_an_expiry_refused_on_edit_is_refused_on_create(client, days):
    """เดิม POST รับ 0 เป็น "ไม่มีวันหมดอายุ" และรับค่าติดลบเป็นนโยบายที่หมดอายุไปแล้วตั้งแต่เกิด"""
    existing = create(client, max_requests=10).json()

    made = create(client, window="month", max_requests=1, expires_in_days=days)
    patched = edit(client, existing["id"], days=days)

    assert (made.status_code, patched.status_code) == (400, 400), (made.text, patched.text)


def test_no_expiry_is_null_either_way(client):
    made = create(client, max_requests=1, expires_in_days=None)
    assert made.status_code == 201 and made.json()["expires_at"] is None

    dated = create(client, window="month", max_requests=1, expires_in_days=3).json()
    assert dated["expires_at"] is not None
    assert edit(client, dated["id"], days=None).json()["expires_at"] is None


def test_a_name_longer_than_the_column_is_refused(client):
    """128 คือความกว้างของคอลัมน์ — เกินกว่านั้น PostgreSQL ตอบ 500 แทนที่จะเป็น 400"""
    assert create(client, name="x" * 129, max_requests=1).status_code == 400
    assert create(client, name="x" * 128, max_requests=1).status_code == 201


# ---------------------------------------------------------------------------
# scope กับเป้าหมาย
# ---------------------------------------------------------------------------
def test_a_user_policy_without_a_user_is_refused_and_binds_nobody(
        client, member_key, upstream):
    """เคสที่ตรวจพบ: นโยบาย "ของคนเดียว" ที่ไม่มีคน = ของทุกคน"""
    made = create(client, scope="user", window="day", max_requests=1, name="for one person")
    assert made.status_code == 400
    assert made.json()["error"]["param"] == "user_id"

    _other, their_key = someone(client)
    for key in (member_key, their_key["api_key"]):
        assert [ask(client, key).status_code for _ in range(2)] == [200, 200]


@pytest.mark.parametrize("body, param", [
    ({"scope": "workspace"}, "workspace_id"),
    ({"scope": "workspace", "workspace_id": ""}, "workspace_id"),
    ({"scope": "key"}, "api_key_id"),
    ({"scope": "user", "user_id": None}, "user_id"),
    ({"scope": "everyone"}, "scope"),
], ids=["workspace", "workspace-empty", "key", "user-null", "unknown-scope"])
def test_a_targeted_scope_needs_its_target(client, body, param):
    made = create(client, max_requests=1, **body)
    assert made.status_code == 400, made.text
    assert made.json()["error"]["param"] == param


def test_a_scope_that_disagrees_with_its_target_is_refused(client):
    """ตัวตัดสินดูที่เป้าหมาย ไม่ได้ดูที่ป้าย scope — "global" ที่มี user_id คือนโยบายของคน
    คนเดียวภายใต้ป้ายที่บอกว่าเป็นของทุกคน"""
    user, key = someone(client)
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "CS101", "name": "CS101"}).json()

    assert create(client, scope="global", user_id=user["id"], max_requests=1).status_code == 400
    assert create(client, scope="global", workspace_id=ws["id"], max_requests=1).status_code == 400
    assert create(client, scope="workspace", workspace_id=ws["id"], user_id=user["id"],
                  max_requests=1).status_code == 400
    assert create(client, scope="user", user_id=user["id"], api_key_id=key["id"],
                  max_requests=1).status_code == 400
    assert create(client, scope="key", api_key_id=key["id"], user_id=user["id"],
                  max_requests=1).status_code == 400


def test_a_key_ceiling_cannot_pretend_to_be_about_one_model(client):
    """เพดานของ key วัดทุกอย่างที่ใบนั้นทำ — รับ model_alias ไว้เฉย ๆ คือป้ายที่โกหก"""
    _user, key = someone(client)
    made = create(client, scope="key", api_key_id=key["id"], model_alias="coding",
                  max_requests=1)
    assert made.status_code == 400
    assert made.json()["error"]["param"] == "model_alias"


def test_scope_is_read_from_the_target_when_it_is_not_sent(client, upstream):
    """`{"user_id": …}` ล้วน ๆ เคยถูกเก็บด้วยป้าย "global" ทั้งที่ผูกคนคนเดียว"""
    user, key = someone(client)
    made = create(client, user_id=user["id"], max_requests=1)

    assert made.status_code == 201
    assert made.json()["scope"] == "user"
    assert [ask(client, key["api_key"]).status_code for _ in range(2)] == [200, 429]

    ceiling = create(client, api_key_id=key["id"], window="month", max_requests=5)
    assert ceiling.json()["scope"] == "key"
    assert create(client, window="month", max_requests=5).json()["scope"] == "global"


@pytest.mark.parametrize("body, status", [
    ({"scope": "user", "user_id": "no-such-user"}, 400),
    ({"scope": "workspace", "workspace_id": "no-such-workspace"}, 400),
    ({"scope": "key", "api_key_id": "no-such-key"}, 400),
    ({"scope": "global", "access_group_id": "no-such-bundle"}, 400),
    ({"scope": "global", "model_alias": "codign"}, 404),
], ids=["user", "workspace", "key", "bundle", "model"])
def test_a_target_that_does_not_exist_is_refused(client, body, status):
    """id ที่ไม่มีอยู่ชน foreign key บน PostgreSQL แล้วตอบ 500 · alias ที่พิมพ์ผิดได้นโยบาย
    ที่ไม่มีวันถูกใช้โดยไม่มีอะไรบอก"""
    made = create(client, max_requests=1, **body)
    assert made.status_code == status, made.text
    assert client.get("/admin/quota-policies",
                      headers=auth(client.admin_key)).json()["data"] == []


def test_what_the_console_sends_is_still_accepted(client):
    """ฟอร์มของคอนโซลส่ง null ให้เป้าหมายที่ไม่ได้เลือก และ 0 ให้ลิมิตที่ไม่ได้กรอก"""
    user, _key = someone(client)
    made = create(
        client, scope="user", workspace_id=None, user_id=user["id"], model_alias=None,
        access_group_id=None, window="day", name="จากฟอร์ม", expires_in_days=None,
        max_requests=100, max_input_tokens=0, max_output_tokens=0, max_images=0,
        max_requests_per_minute=0, max_tokens_per_minute=0,
    )
    assert made.status_code == 201, made.text
    assert set(made.json()) == {
        "id", "name", "scope", "workspace_id", "user_id", "api_key_id", "model_alias",
        "access_group_id", "window", *LIMITS, "expires_at", "effective", "shadowed_by",
    }
