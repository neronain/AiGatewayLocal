"""manager ใส่ผู้ดูแลระบบเข้า workspace ของตัวเองไม่ได้ — ผู้ดูแลเท่านั้นที่ใส่ผู้ดูแล

เคสจริง (ผู้ตรวจอิสระ 2026-10-09): ขั้นที่สองของการยกระดับสิทธิ์ที่ปิดไปใน dca222c คือ

    POST /admin/workspaces/<ของ manager>/join {"user_id": <admin>}   200

manager ได้ user_id ของ admin มาจาก `GET /v1/me` ด้วย key ที่ admin ออกให้สคริปต์ของวิชา ·
หลัง dca222c การใส่เข้าไม่นำไปสู่การแก้ key ของ admin อีกแล้ว แต่ยังทำให้ admin โผล่ใน
มุมมองของ manager ทั้งหมด: แถวผู้ใช้ · รายการ key (ชื่อ prefix ข้อจำกัด) · การใช้งานและ
โควตา — ทั้งที่ membership ไม่ได้ให้อะไรกับ admin เลย (admin ไม่ขึ้นกับ membership) และ
admin ใส่ตัวเองได้ถ้าต้องการ · เจ้าของงานเคาะ 2026-10-09: ปฏิเสธ

กติกา (`_assert_may_enrol` ใน app/api/admin.py — ใช้กับทุกเส้นทางที่ *เพิ่ม* สมาชิก):
คนที่ไม่ใช่ผู้ดูแล เพิ่มผู้ใช้ที่เป็นผู้ดูแลเข้า workspace ไม่ได้ · ไม่แตะ membership ที่มีอยู่
แล้ว · การ *เอาออก* ยังทำได้ (เอาออกคือลดสิ่งที่มองเห็น) · manager ใส่ manager อีกคนยังทำได้
— นั่นคือวิธีเพิ่มผู้ช่วยสอน และสิ่งที่ผู้ใส่ได้เพิ่มถูกกันด้วยกติกาของ key อยู่แล้ว
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Membership
from app.db.session import session_scope
from tests.test_a_limited_key_has_no_admin_power import (
    admin,
    call,
    catalogue,
    key_for,
    owner_of,
    person,
    sign_in,
    workspace,
)

ALL = ["coding", "gemma-vision", "muse-local"]


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def as_person(client, username, method, path, **kw):
    """คำขอจากคอนโซลของคนนั้น — session ไม่ใช่ key"""
    sign_in(client, username)
    try:
        return client.request(method, path, **kw)
    finally:
        client.cookies.clear()


def members(client, ws) -> set[str]:
    async def read():
        async with session_scope() as session:
            return {row[0] for row in await session.execute(
                select(Membership.user_id).where(Membership.workspace_id == ws["id"])
            )}

    return client.portal.call(read)


@pytest.fixture
def cs101(client):
    boss = person(client, "boss", "manager")
    ws = workspace(client, "CS101", ALL)
    assert admin(client, "POST", f"/admin/workspaces/{ws['id']}/join",
                 json={"user_id": boss["id"]}).status_code == 200
    return {"boss": boss, "ws": ws, "root": owner_of(client, client.admin_key)}


# วิธีเพิ่มสมาชิกด้วย id มีสองเส้นทาง — ทุกข้อข้างล่างที่พูดถึง "ใส่เข้า" ลองทั้งคู่
ADD = {
    "join": lambda ws, *ids: ("POST", f"/admin/workspaces/{ws['id']}/join",
                              {"user_id": ids[0]}),
    "members": lambda ws, *ids: ("POST", f"/admin/workspaces/{ws['id']}/members",
                                 {"user_ids": list(ids)}),
}


def add(client, how, ws, *ids, by=None):
    method, path, body = ADD[how](ws, *ids)
    if by is None:
        return admin(client, method, path, json=body)
    return as_person(client, by, method, path, json=body)


# ── ตัว defect ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("how", sorted(ADD))
def test_a_manager_cannot_add_an_administrator_to_their_workspace(client, cs101, how):
    response = add(client, how, cs101["ws"], cs101["root"], by="boss")

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert cs101["root"] not in members(client, cs101["ws"])


def test_what_the_manager_would_have_seen_stays_out_of_their_views(client, cs101):
    """สิ่งที่การใส่เข้าเคยให้: แถวผู้ใช้ของ admin และรายการ key ของเขา ในหน้าของ manager"""
    script = key_for(client, cs101["root"], models=["coding"])

    add(client, "join", cs101["ws"], cs101["root"], by="boss")

    users = as_person(client, "boss", "GET", "/admin/users").json()["data"]
    keys = as_person(client, "boss", "GET", "/admin/api-keys").json()["data"]
    assert cs101["root"] not in {u["id"] for u in users}
    assert script["id"] not in {k["id"] for k in keys}
    assert as_person(client, "boss", "GET",
                     f"/admin/users/{cs101['root']}/quota").status_code != 200


def test_one_administrator_in_a_list_refuses_the_whole_list(client, cs101):
    """เหมือน id ที่ไม่มีอยู่จริง: รายชื่อที่มีปัญหาไม่ถูกทำไปครึ่งเดียว"""
    student = person(client, "s1", "member")

    response = add(client, "members", cs101["ws"], student["id"], cs101["root"], by="boss")

    assert response.status_code == 403, response.text
    assert members(client, cs101["ws"]) == {cs101["boss"]["id"]}
    assert "test-admin" in response.json()["error"]["message"], "ต้องบอกว่าติดที่ใคร"


def test_a_managers_own_unlimited_key_is_held_to_the_same_rule(client, cs101):
    """สคริปต์ของ manager (key ไม่จำกัด) ทำได้เท่าที่ตัวเขาทำได้ในคอนโซล ไม่มากกว่า"""
    script = key_for(client, cs101["boss"])["api_key"]

    response = call(client, script, "POST", f"/admin/workspaces/{cs101['ws']['id']}/join",
                    json={"user_id": cs101["root"]})

    assert response.status_code == 403
    assert cs101["root"] not in members(client, cs101["ws"])


def test_the_refusal_says_who_can_do_it_instead(client, cs101):
    error = add(client, "join", cs101["ws"], cs101["root"], by="boss").json()["error"]

    message = error["message"]
    assert "test-admin" in message and "administrator" in message
    assert "Only an administrator can add" in message
    assert error["details"] == {"reason_code": "enrol_administrator",
                                "administrators": ["test-admin"]}


def test_the_chain_from_holding_a_limited_admin_key_now_stops_at_the_first_step(client):
    """ลำดับของผู้ตรวจ ตั้งแต่ต้นจนจบ — หยุดที่ join และยังหยุดที่ PATCH ด้วย"""
    boss = person(client, "boss", "manager")
    ws = workspace(client, "CS101", ALL)
    add(client, "join", ws, boss["id"])
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])
    k = limited["api_key"]

    whose = call(client, k, "GET", "/v1/me").json()["user_id"]
    joined = add(client, "join", ws, whose, by="boss")
    listed = as_person(client, "boss", "GET", "/admin/api-keys").json()["data"]
    lifted = as_person(client, "boss", "PATCH", f"/admin/api-keys/{limited['id']}",
                       json={"models": []})

    assert joined.status_code == 403, "ขั้นที่หนึ่ง: ใส่ admin เข้าวิชาตัวเอง"
    assert limited["id"] not in {row["id"] for row in listed}, "ขั้นที่สอง: เห็น key ของ admin"
    assert lifted.status_code in (400, 403), "ขั้นที่สาม: ถอดรายการ"
    assert call(client, k, "GET", "/admin/secrets").status_code == 403
    assert call(client, k, "POST", "/admin/users",
                json={"external_id": "made-by-escalated-key", "role": "admin"}).status_code == 403
    assert catalogue(client, k) == {"coding"}


# ── สิ่งที่ต้องไม่เปลี่ยน ──────────────────────────────────────────────────────

@pytest.mark.parametrize("how", sorted(ADD))
def test_an_administrator_adds_an_administrator(client, cs101, how):
    """ใส่ตัวเอง หรือใส่ผู้ดูแลอีกคน — เป็นเรื่องของผู้ดูแล และยังทำได้"""
    other = person(client, "root2", "admin")

    assert add(client, how, cs101["ws"], cs101["root"]).status_code == 200       # ตัวเอง
    assert add(client, how, cs101["ws"], other["id"]).status_code == 200         # อีกคน

    assert {cs101["root"], other["id"]} <= members(client, cs101["ws"])


@pytest.mark.parametrize("how", sorted(ADD))
def test_a_manager_still_enrols_members(client, cs101, how):
    student = person(client, "s1", "member")

    response = add(client, how, cs101["ws"], student["id"], by="boss")

    assert response.status_code == 200, response.text
    assert student["id"] in members(client, cs101["ws"])


@pytest.mark.parametrize("how", sorted(ADD))
def test_a_manager_still_enrols_another_manager(client, cs101, how):
    """ตัดสินไว้: ยังทำได้ — นี่คือวิธีเพิ่มผู้ช่วยสอน

    สิ่งที่ผู้ใส่ได้เพิ่มจากการใส่ manager อีกคน คือสิ่งเดียวกับที่ได้จากการใส่สมาชิก (เห็น
    แถวผู้ใช้ · ออก key *ที่จำกัด* ในชื่อเขา) · สิทธิ์ manager ของคนที่ถูกใส่ไม่ตามมาด้วย:
    key ไม่จำกัดในชื่อเขาออกไม่ได้ และ key ไม่จำกัดที่เขามีอยู่แตะไม่ได้ (dca222c)
    """
    colleague = person(client, "ta", "manager")
    art = workspace(client, "ART200", ALL)
    add(client, "join", art, colleague["id"])
    theirs = key_for(client, colleague)

    response = add(client, how, cs101["ws"], colleague["id"], by="boss")

    assert response.status_code == 200, response.text
    assert colleague["id"] in members(client, cs101["ws"])
    # ผู้ถูกใส่กลายเป็นผู้ดูแลร่วมของ CS101 — ผู้ใส่ไม่ได้อะไรจาก ART200
    seen = {w["code"] for w in as_person(client, "boss", "GET", "/admin/workspaces").json()["data"]}
    assert seen == {"CS101"}
    assert as_person(client, "boss", "POST", "/admin/api-keys",
                     json={"user_id": colleague["id"], "name": "x"}).status_code == 403
    for method, body in (("PATCH", {"models": ["coding"]}), ("DELETE", None)):
        assert as_person(client, "boss", method, f"/admin/api-keys/{theirs['id']}",
                         json=body).status_code == 403


def test_a_manager_can_still_remove_an_administrator_who_is_already_in(client, cs101):
    """เอาออกคือลดสิ่งที่มองเห็น — ไม่มีเหตุให้กัน และต้องเป็นทางออกของสถานะที่มีอยู่ก่อนอัปเกรด"""
    add(client, "join", cs101["ws"], cs101["root"])            # ผู้ดูแลใส่ตัวเองไว้ก่อน
    assert cs101["root"] in members(client, cs101["ws"])

    response = as_person(client, "boss", "DELETE",
                         f"/admin/workspaces/{cs101['ws']['id']}/members/{cs101['root']}")

    assert response.status_code == 200, response.text
    assert cs101["root"] not in members(client, cs101["ws"])


@pytest.mark.parametrize("how", sorted(ADD))
def test_an_administrator_who_is_already_a_member_does_not_break_a_rerun(client, cs101, how):
    """membership ที่มีอยู่ไม่ถูกแตะ และรายชื่อที่รันซ้ำต้องยังรันได้ (ดู `add_members`)

    กติกาห้าม *เพิ่ม* · ผู้ดูแลที่อยู่ในวิชาอยู่แล้วไม่ได้ถูกเพิ่ม — manager ส่งรายชื่อเดิมซ้ำ
    หลังล้มครึ่งทางต้องไม่เจอ 403 เพราะชื่อที่ไม่มีอะไรจะทำกับมัน
    """
    add(client, "join", cs101["ws"], cs101["root"])
    student = person(client, "s1", "member")

    response = add(client, how, cs101["ws"], cs101["root"], by="boss")
    assert response.status_code == 200, response.text
    if how == "members":
        both = add(client, how, cs101["ws"], cs101["root"], student["id"], by="boss")
        assert both.status_code == 200, both.text
        assert both.json()["added"] == 1 and both.json()["already_in"] == 1

    assert cs101["root"] in members(client, cs101["ws"])


def test_the_users_list_shows_an_administrator_who_is_in_a_workspace(client, cs101):
    """สถานะที่มีอยู่ก่อนอัปเกรดต้องมองเห็นได้จากหน้าที่มีอยู่: Access → Users"""
    add(client, "join", cs101["ws"], cs101["root"])

    rows = {u["id"]: u for u in admin(client, "GET", "/admin/users").json()["data"]}

    assert rows[cs101["root"]]["role"] == "admin"
    assert rows[cs101["root"]]["workspaces"] == ["CS101"]
