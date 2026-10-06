"""ต้องเหลือ admin ที่ *ใช้งานได้* อย่างน้อยหนึ่งคนเสมอ — ไม่ว่าจะเปลี่ยนช่องไหน

เคสจริง (ตรวจ 2026-10-06 บน a5e9295): ลดขั้น admin คนสุดท้ายถูกปฏิเสธ 400 ตามที่ควร แต่
`PATCH /admin/users/<admin คนเดียว> {"status": "Suspended"}` ได้ 200 · หลังจากนั้นทุกคำขอ
ของผู้ดูแลได้ 403 "This account is not active" และ `/auth/status` ยังบอก
`needs_setup: false` — เครื่องถูกล็อกจากข้างใน ไม่มีทางกลับผ่านคอนโซล ต้องแก้ฐานข้อมูลเอง

สองรูในด่านเดียว: มันดูแค่ `role` ไม่ดู `status` · และ `status` รับสตริงอะไรก็ได้
("Suspended" ตัว S ใหญ่ถูกบันทึกลงไปทั้งอย่างนั้น — ระงับบัญชีได้จริงเพราะ auth เทียบ
`!= "active"` แต่ไม่มีอะไรในระบบรู้จักค่านี้)
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def users(client) -> dict[str, dict]:
    return {u["external_id"]: u for u in admin(client, "GET", "/admin/users").json()["data"]}


def patch(client, user_id, **body):
    return admin(client, "PATCH", f"/admin/users/{user_id}", json=body)


@pytest.fixture
def sole_admin(client):
    """เหลือ admin คนเดียว (เจ้าของ key ของเทส) — startup สร้าง `admin` มาอีกคน"""
    everyone = users(client)
    assert patch(client, everyone["admin"]["id"], role="member").status_code == 200
    return everyone["test-admin"]


# ── ตัว defect ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("change", [
    {"status": "suspended"},
    {"role": "member"},
    {"role": "manager"},
    {"role": "member", "status": "suspended"},
    {"role": "admin", "status": "suspended"},
])
def test_the_only_active_admin_cannot_be_taken_out_by_any_field(client, sole_admin, change):
    response = patch(client, sole_admin["id"], **change)

    assert response.status_code == 400, response.text
    assert "only administrator" in response.json()["error"]["message"].lower()
    # สิ่งที่สำคัญจริง: ผู้ดูแลยังทำงานได้
    still = users(client)["test-admin"]
    assert (still["role"], still["status"]) == ("admin", "active")
    assert admin(client, "GET", "/admin/workspaces").status_code == 200


def test_a_suspended_admin_does_not_count_as_the_one_left(client):
    """ด่านเดิมนับ admin ทุกคนไม่ว่าสถานะอะไร: เหลือ admin ที่ถูกระงับอยู่หนึ่งคน
    ก็ถือว่า "ยังมีอีกคน" แล้วปล่อยให้คนสุดท้ายที่ใช้งานได้ลดขั้นตัวเอง"""
    everyone = users(client)
    assert patch(client, everyone["admin"]["id"], status="suspended").status_code == 200

    response = patch(client, everyone["test-admin"]["id"], role="member")

    assert response.status_code == 400, response.text
    assert users(client)["test-admin"]["role"] == "admin"


def test_one_of_two_active_admins_can_be_suspended_or_demoted(client):
    """กันไว้เพื่อไม่ให้ล็อกเครื่อง ไม่ใช่ห้ามแตะ admin"""
    everyone = users(client)
    assert patch(client, everyone["admin"]["id"], status="suspended").status_code == 200
    assert users(client)["admin"]["status"] == "suspended"
    # เปิดกลับ แล้วลดขั้นแทน
    assert patch(client, everyone["admin"]["id"], status="active").status_code == 200
    assert patch(client, everyone["admin"]["id"], role="member").status_code == 200
    assert admin(client, "GET", "/admin/users").status_code == 200


def test_an_admin_who_is_already_suspended_can_still_be_edited(client):
    """คนที่ไม่ได้นับอยู่แล้ว เปลี่ยนอะไรก็ไม่ทำให้จำนวนลด"""
    everyone = users(client)
    other = everyone["admin"]["id"]
    patch(client, other, status="suspended")
    assert patch(client, other, role="member").status_code == 200
    assert patch(client, other, display_name="Former admin").status_code == 200


def test_changing_something_else_on_the_only_admin_is_fine(client, sole_admin):
    assert patch(client, sole_admin["id"], display_name="Root").status_code == 200
    assert patch(client, sole_admin["id"], role="admin", status="active").status_code == 200


def test_members_and_managers_can_be_suspended_and_brought_back(client, member_key):
    somchai = users(client)["6412345678"]

    assert patch(client, somchai["id"], status="suspended").status_code == 200
    assert client.get("/v1/me", headers=auth(member_key)).status_code == 403
    assert patch(client, somchai["id"], status="active").status_code == 200
    assert client.get("/v1/me", headers=auth(member_key)).status_code == 200


# ── status รับเฉพาะค่าที่ระบบรู้จัก ตรงตัวอักษร ──────────────────────────────

@pytest.mark.parametrize("bad", ["Suspended", "ACTIVE", "Active", " active", "disabled",
                                 "banned", "", None, 1, True, ["active"]])
def test_a_status_the_system_does_not_use_is_refused(client, member_key, bad):
    somchai = users(client)["6412345678"]

    response = patch(client, somchai["id"], status=bad)

    assert response.status_code == 400, response.text
    assert "active, suspended" in response.json()["error"]["message"]
    assert users(client)["6412345678"]["status"] == "active"
    assert client.get("/v1/me", headers=auth(member_key)).status_code == 200


@pytest.mark.parametrize("bad", ["Suspended", "Active", "disabled", ""])
def test_a_user_cannot_be_created_with_a_status_the_system_does_not_use(client, bad):
    response = admin(client, "POST", "/admin/users",
                     json={"external_id": "new-person", "status": bad})
    assert response.status_code == 400, response.text
    assert "new-person" not in users(client)


@pytest.mark.parametrize("status", ["active", "suspended"])
def test_both_real_statuses_are_accepted(client, status):
    made = admin(client, "POST", "/admin/users",
                 json={"external_id": "new-person", "status": status})
    assert made.status_code == 201, made.text
    assert users(client)["new-person"]["status"] == status


def test_the_misspelling_that_locked_an_install_out_now_changes_nothing(client, sole_admin):
    """repro ของผู้ตรวจตรง ๆ: "Suspended" ใส่ admin คนเดียว"""
    gone = patch(client, sole_admin["id"], status="Suspended")

    assert gone.status_code == 400
    assert admin(client, "GET", "/admin/users").status_code == 200
    assert users(client)["test-admin"]["status"] == "active"
