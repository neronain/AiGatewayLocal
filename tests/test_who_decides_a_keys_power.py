"""ใครตัดสินได้ว่า key ใบหนึ่งพกสิทธิ์ผู้ดูแลของเจ้าของหรือไม่ — ไม่ใช่ manager ของคนอื่น

เคสจริง (ผู้ตรวจอิสระ 2026-10-09 บน branch ที่รวมแล้ว): ผู้ดูแลออก key บนบัญชี admin จำกัด
ไว้ที่ `coding` ให้สคริปต์ของวิชาหนึ่ง · manager M ของวิชานั้นถือใบนั้นอยู่ —

    GET   /v1/me            (ด้วยใบนั้น)        → user_id ของ admin
    POST  /admin/workspaces/<ของ M>/join        200  ← ใส่ admin เข้าวิชาตัวเอง
    GET   /admin/api-keys                       เห็น key ของ admin
    PATCH /admin/api-keys/<ใบนั้น> {"models": []} 200  ← ถอดรายการ
    GET   /admin/secrets    (ด้วยใบนั้น)        200
    POST  /admin/users {"role": "admin"}        201

กติกา "ใบที่จำกัดไว้ไม่พกสิทธิ์ผู้ดูแล" ทำให้ *การถอดข้อจำกัด* กลายเป็นการให้สิทธิ์ผู้ดูแล
แต่ `PATCH /admin/api-keys` ยังตรวจการถอดรายการด้วยคำถามเดียว — "โมเดลที่ใบจะเรียกได้
manager คนนี้เรียกเองได้ไหม" · ตอนออก key มีด่าน "Only an admin can issue an admin key"
ตอนแก้ไม่มี · คันโยกเดียวกันกลับด้าน: M ใส่รายการให้ใบ admin ที่ *ไม่จำกัด* ของคนอื่น
แล้วงานดูแลระบบของเขาได้ 403 เงียบ ๆ

ไล่ต่อเจออีกทางที่ไม่ต้องมีใบในมือด้วยซ้ำ (ตรวจ 2026-10-09 บน 022998d): M1 ดูแล CS101
อย่างเดียว · M2 เป็น manager ที่อยู่ทั้ง CS101 และ ART200 · M1 ออก key ไม่จำกัดให้ M2 ได้
201 พร้อมตัว key — ใบนั้น `GET /admin/workspaces` เห็น ART200

กติกา (ที่เดียว: `_assert_may_decide_key` ใน app/api/admin.py):
  1. key ของ admin — ออก · แก้ · เพิกถอน ได้เฉพาะ admin
  2. key ของ manager ที่ไม่มีข้อจำกัด (หรือจะไม่มีหลังแก้) พกสิทธิ์ manager ของเจ้าของ —
     ออก · แก้ · เพิกถอน ได้เฉพาะ admin หรือเจ้าของเอง
  3. นอกนั้นเหมือนเดิม: manager ยังออก/แก้/เพิกถอน key ของสมาชิกในกลุ่มตัวเอง และ key ที่
     จำกัดไว้ (และยังจำกัดอยู่หลังแก้) ของ manager ด้วยกัน
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import ApiKey, utcnow
from app.db.session import session_scope
from tests.test_a_limited_key_has_no_admin_power import (
    admin,
    call,
    catalogue,
    key_for,
    owner_of,
    person,
    sign_in,
    stored,
    workspace,
)

ALL = ["coding", "gemma-vision", "muse-local"]


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def join(client, ws, *people):
    for who in people:
        user_id = who if isinstance(who, str) else who["id"]
        done = admin(client, "POST", f"/admin/workspaces/{ws['id']}/join",
                     json={"user_id": user_id})
        assert done.status_code == 200, done.text


def as_person(client, username, method, path, **kw):
    """คำขอจากคอนโซลของคนนั้น — session ไม่ใช่ key"""
    sign_in(client, username)
    try:
        return client.request(method, path, **kw)
    finally:
        client.cookies.clear()


def set_key(client, key_id, **values) -> None:
    async def write():
        async with session_scope() as session:
            row = await session.get(ApiKey, key_id)
            for name, value in values.items():
                setattr(row, name, value)

    client.portal.call(write)


@pytest.fixture
def cs101(client):
    """วิชาที่เปิดทุกโมเดล — ด่าน "ให้ได้เท่าที่ตัวเองเรียกได้" จึงไม่ใช่สิ่งที่หยุด manager"""
    boss = person(client, "boss", "manager")
    root = owner_of(client, client.admin_key)
    ws = workspace(client, "CS101", ALL)
    # ผู้ดูแลอยู่ในวิชาของ manager — จะเข้ามาด้วยมือใครก็ตาม key ของเขาอยู่ในสายตา manager
    join(client, ws, boss, root)
    return {"boss": boss, "root": root, "ws": ws}


# ── ตัว defect: manager กับ key ของ admin ─────────────────────────────────────

def test_a_manager_cannot_lift_the_list_on_an_admins_key(client, cs101):
    limited = key_for(client, cs101["root"], models=["coding"])
    assert call(client, limited["api_key"], "GET", "/admin/secrets").status_code == 403

    response = as_person(client, "boss", "PATCH", f"/admin/api-keys/{limited['id']}",
                         json={"models": []})

    assert response.status_code == 403, response.text
    assert list(stored(client, limited["id"]).models) == ["coding"]
    assert call(client, limited["api_key"], "GET", "/admin/secrets").status_code == 403
    made = call(client, limited["api_key"], "POST", "/admin/users",
                json={"external_id": "made-by-escalated-key", "role": "admin"})
    assert made.status_code == 403


def test_the_whole_chain_from_holding_the_key_ends_nowhere(client):
    """ลำดับเดียวกับที่ผู้ตรวจทำ — manager เริ่มจากมีแค่ใบที่จำกัดไว้ กับสิทธิ์ manager ของตัวเอง"""
    boss = person(client, "boss", "manager")
    ws = workspace(client, "CS101", ALL)
    join(client, ws, boss)
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])
    k = limited["api_key"]

    whose = call(client, k, "GET", "/v1/me").json()["user_id"]
    # ใส่ admin เข้าวิชาตัวเอง: ยังทำได้ (เป็นเรื่องให้เจ้าของงานเคาะ) — ที่ต้องไม่ได้คือผลของมัน
    as_person(client, "boss", "POST", f"/admin/workspaces/{ws['id']}/join",
              json={"user_id": whose})
    lifted = as_person(client, "boss", "PATCH", f"/admin/api-keys/{limited['id']}",
                       json={"models": []})

    assert lifted.status_code in (400, 403), lifted.text
    assert call(client, k, "GET", "/admin/secrets").status_code == 403
    assert catalogue(client, k) == {"coding"}


def test_a_manager_cannot_put_a_list_on_an_admins_unlimited_key(client, cs101):
    """คันโยกเดียวกันกลับด้าน: งานดูแลระบบของคนอื่นได้ 403 โดยเจ้าของไม่รู้"""
    plain = key_for(client, cs101["root"])

    response = as_person(client, "boss", "PATCH", f"/admin/api-keys/{plain['id']}",
                         json={"models": ["coding"]})

    assert response.status_code == 403, response.text
    assert list(stored(client, plain["id"]).models) == []
    assert call(client, plain["api_key"], "GET", "/admin/users").status_code == 200


def test_a_manager_cannot_bring_an_expired_admin_key_back(client, cs101):
    """ใบ admin ที่หมดอายุแล้วหยุดทำงาน — ต่ออายุคือคืนสิทธิ์ผู้ดูแลให้ใบนั้นทั้งใบ"""
    old = key_for(client, cs101["root"])
    set_key(client, old["id"], expires_at=utcnow() - timedelta(days=1))
    assert call(client, old["api_key"], "GET", "/admin/users").status_code == 401

    response = as_person(client, "boss", "PATCH", f"/admin/api-keys/{old['id']}",
                         json={"days": 30})

    assert response.status_code == 403, response.text
    assert call(client, old["api_key"], "GET", "/admin/users").status_code == 401


def test_a_manager_cannot_revoke_an_admins_key(client, cs101):
    """เพิกถอนคือ "ใส่ข้อจำกัด" แบบที่กู้ไม่ได้ — กันแก้ไว้แล้วปล่อยเพิกถอน ด่านก็ไม่มีความหมาย"""
    plain = key_for(client, cs101["root"])

    response = as_person(client, "boss", "DELETE", f"/admin/api-keys/{plain['id']}")

    assert response.status_code == 403, response.text
    assert call(client, plain["api_key"], "GET", "/admin/users").status_code == 200


def test_an_admin_still_does_all_of_it(client, cs101):
    limited, plain = (key_for(client, cs101["root"], models=["coding"]),
                      key_for(client, cs101["root"]))

    assert admin(client, "PATCH", f"/admin/api-keys/{limited['id']}",
                 json={"models": []}).status_code == 200
    assert call(client, limited["api_key"], "GET", "/admin/secrets").status_code == 200
    assert admin(client, "PATCH", f"/admin/api-keys/{plain['id']}",
                 json={"models": ["coding"], "days": 30}).status_code == 200
    assert admin(client, "DELETE", f"/admin/api-keys/{plain['id']}").status_code == 200


# ── manager กับ key ของ manager อีกคน ─────────────────────────────────────────

@pytest.fixture
def two_managers(client):
    """M1 ดูแล CS101 · M2 อยู่ CS101 ด้วย และดูแล ART200 ที่ M1 ไม่เกี่ยว

    สองวิชาเปิดโมเดลชุดเดียวกัน — ด่าน "ให้ได้เท่าที่ตัวเองเรียกได้" จึงผ่านทุกข้อ และสิ่งเดียว
    ที่ต่างกันระหว่างสองคนคือ *วิชาที่ดูแล* ซึ่งด่านนั้นไม่เคยดู
    """
    m1, m2 = person(client, "m1", "manager"), person(client, "m2", "manager")
    cs, art = workspace(client, "CS101", ALL), workspace(client, "ART200", ALL)
    join(client, cs, m1, m2)
    join(client, art, m2)
    return {"m1": m1, "m2": m2, "cs": cs, "art": art}


def _manages(client, key: str) -> set[str]:
    response = call(client, key, "GET", "/admin/workspaces")
    return {w["code"] for w in response.json()["data"]} if response.status_code == 200 else set()


def test_a_manager_cannot_issue_a_key_that_manages_more_than_they_do(client, two_managers):
    """ไม่ต้องมีใบในมือด้วยซ้ำ: ออกใบไม่จำกัดให้ manager อีกคน แล้วได้ตัว key กลับมาเอง"""
    m2 = two_managers["m2"]

    response = as_person(client, "m1", "POST", "/admin/api-keys",
                         json={"user_id": m2["id"], "name": "for m2"})

    assert response.status_code == 403, response.text
    assert "api_key" not in response.json()
    names = {k["name"] for k in admin(client, "GET", "/admin/api-keys").json()["data"]}
    assert "for m2" not in names


def test_a_limited_key_for_another_manager_can_still_be_issued(client, two_managers):
    """ใบที่มีรายการโมเดลไม่พกสิทธิ์ของใคร — manager ออกให้เพื่อนร่วมวิชาได้เหมือนเดิม"""
    m2 = two_managers["m2"]

    response = as_person(client, "m1", "POST", "/admin/api-keys",
                         json={"user_id": m2["id"], "name": "script", "models": ["coding"]})

    assert response.status_code == 201, response.text
    key = response.json()["api_key"]
    assert catalogue(client, key) == {"coding"}
    assert _manages(client, key) == set()


@pytest.mark.parametrize("change", [
    {"models": []},                  # ถอดรายการ: ใบได้สิทธิ์ manager ของ M2 กลับมา
    {"models": ALL},                 # ยังมีรายการ แต่ผ่านไปดูว่าไม่ถูกปฏิเสธเกินเหตุ (ดู assert)
], ids=["lift", "widen but still listed"])
def test_lifting_the_list_on_another_managers_key_is_not_a_managers_call(
        client, two_managers, change):
    limited = key_for(client, two_managers["m2"], models=["coding"])

    response = as_person(client, "m1", "PATCH", f"/admin/api-keys/{limited['id']}", json=change)

    if change["models"]:
        # ใบยังจำกัดอยู่หลังแก้ = ไม่มีสิทธิ์ของใครเปลี่ยนมือ — เรื่องนี้ manager ทำได้เหมือนเดิม
        assert response.status_code == 200, response.text
        assert _manages(client, limited["api_key"]) == set()
    else:
        assert response.status_code == 403, response.text
        assert list(stored(client, limited["id"]).models) == ["coding"]
        assert _manages(client, limited["api_key"]) == set()


@pytest.mark.parametrize("method, body", [
    ("PATCH", {"models": ["coding"]}),
    ("PATCH", {"days": 3650}),
    ("DELETE", None),
], ids=["narrow", "extend", "revoke"])
def test_another_managers_unlimited_key_is_not_a_managers_to_touch(
        client, two_managers, method, body):
    plain = key_for(client, two_managers["m2"])
    before = stored(client, plain["id"])

    response = as_person(client, "m1", method, f"/admin/api-keys/{plain['id']}", json=body)

    assert response.status_code == 403, response.text
    after = stored(client, plain["id"])
    assert (list(after.models), after.expires_at, after.revoked_at) == (
        list(before.models), before.expires_at, before.revoked_at)
    assert _manages(client, plain["api_key"]) == {"CS101", "ART200"}


def test_a_key_that_stays_limited_by_something_else_can_have_its_list_lifted(
        client, two_managers):
    """ผูก workspace อยู่ = ถอดรายการแล้วก็ยังเป็นใบที่จำกัด ไม่มีสิทธิ์ของใครกลับมา"""
    bound = key_for(client, two_managers["m2"], models=["coding"],
                    workspace_id=two_managers["cs"]["id"])

    response = as_person(client, "m1", "PATCH", f"/admin/api-keys/{bound['id']}",
                         json={"models": []})

    assert response.status_code == 200, response.text
    assert _manages(client, bound["api_key"]) == set()


def test_a_cap_that_is_the_only_limit_counts_when_deciding(client, two_managers):
    """ใบที่มีแต่เพดานของตัวเองคือใบที่จำกัด — ใส่รายการเพิ่มไม่ได้เปลี่ยนสิทธิ์ของใคร"""
    capped = key_for(client, two_managers["m2"])
    made = admin(client, "POST", "/admin/quota-policies",
                 json={"scope": "key", "api_key_id": capped["id"], "name": "cap",
                       "window": "day", "max_requests": 100})
    assert made.status_code == 201, made.text

    response = as_person(client, "m1", "PATCH", f"/admin/api-keys/{capped['id']}",
                         json={"models": ["coding"]})

    assert response.status_code == 200, response.text


# ── สิ่งที่ต้องไม่เปลี่ยน ──────────────────────────────────────────────────────

def test_a_manager_decides_for_their_own_keys(client, two_managers):
    """เจ้าของมีสิทธิ์นั้นอยู่แล้วในคอนโซล — ถอดรายการของใบตัวเองไม่ได้ให้อะไรเพิ่ม"""
    m1 = two_managers["m1"]
    issued = as_person(client, "m1", "POST", "/admin/api-keys",
                       json={"user_id": m1["id"], "name": "mine"})
    assert issued.status_code == 201, issued.text
    mine = issued.json()
    assert _manages(client, mine["api_key"]) == {"CS101"}

    for body, manages in (({"models": ["coding"]}, set()), ({"models": []}, {"CS101"}),
                          ({"days": 30}, {"CS101"})):
        done = as_person(client, "m1", "PATCH", f"/admin/api-keys/{mine['id']}", json=body)
        assert done.status_code == 200, done.text
        assert _manages(client, mine["api_key"]) == manages

    assert as_person(client, "m1", "DELETE",
                     f"/admin/api-keys/{mine['id']}").status_code == 200


def test_a_manager_still_runs_the_keys_of_the_members_in_their_class(client, two_managers):
    """สมาชิกไม่มีสิทธิ์ผู้ดูแลให้ใครได้หรือเสีย — งานประจำของ manager ต้องเหมือนเดิมทุกอย่าง"""
    student = person(client, "s1", "member")
    join(client, two_managers["cs"], student)

    issued = as_person(client, "m1", "POST", "/admin/api-keys",
                       json={"user_id": student["id"], "name": "lab"})
    assert issued.status_code == 201, issued.text
    key = issued.json()

    for body in ({"models": ["coding"]}, {"models": []}, {"days": 30}):
        done = as_person(client, "m1", "PATCH", f"/admin/api-keys/{key['id']}", json=body)
        assert done.status_code == 200, done.text
    assert as_person(client, "m1", "DELETE",
                     f"/admin/api-keys/{key['id']}").status_code == 200


def test_a_managers_own_unlimited_key_does_what_their_console_does(client, two_managers):
    """สคริปต์ของ manager เอง (ใบไม่จำกัด) ออก key ให้ตัวเองและนักเรียนได้ตามเดิม"""
    m1 = two_managers["m1"]
    script = key_for(client, m1)["api_key"]

    assert call(client, script, "POST", "/admin/api-keys",
                json={"user_id": m1["id"], "name": "another"}).status_code == 201
    assert call(client, script, "POST", "/admin/api-keys",
                json={"user_id": two_managers["m2"]["id"], "name": "x"}).status_code == 403


# ── คำปฏิเสธ ──────────────────────────────────────────────────────────────────

def test_the_refusal_for_an_admins_key_says_who_can(client, cs101):
    limited = key_for(client, cs101["root"], models=["coding"])

    error = as_person(client, "boss", "PATCH", f"/admin/api-keys/{limited['id']}",
                      json={"models": []}).json()["error"]

    assert error["code"] == "INSUFFICIENT_SCOPE"
    assert error["message"] == "Only an admin can change an admin key."


def test_the_refusal_for_a_managers_key_says_why_and_what_to_do(client, two_managers):
    error = as_person(client, "m1", "POST", "/admin/api-keys",
                      json={"user_id": two_managers["m2"]["id"]}).json()["error"]

    assert error["code"] == "INSUFFICIENT_SCOPE"
    message = error["message"]
    assert "m2" in message and "manager rights" in message, "ต้องบอกว่าใบแบบนี้พกสิทธิ์ของใคร"
    assert "administrator" in message, "ทางออกที่หนึ่ง: ให้ admin ทำ"
    assert "model list" in message, "ทางออกที่สอง: ออกเป็นใบที่จำกัด"
    assert error["details"] == {"reason_code": "key_carries_rights", "owner_role": "manager"}
