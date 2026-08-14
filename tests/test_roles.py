"""Changing someone's role, and the two ways that could go wrong quietly.

Roles could be set when a person was created and never afterwards — the console
showed people only as options in the key-issuing dropdown. Worse, nothing checked
the value: `Principal.is_admin` compares `role == "admin"` exactly, so "Admin" or
"adminn" saved fine and left that person a member, with nothing to say so.

And an administrator can hand away their own admin role. With none left, nobody
can issue a key, set a quota or reload the registry, and the console offers no
way back — it has to be fixed in the database.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _make(client, external_id, role="member"):
    response = client.post(
        "/admin/users",
        headers=auth(client.admin_key),
        json={"external_id": external_id, "display_name": external_id, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _role_of(client, user_id) -> str:
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    return next(u["role"] for u in users if u["id"] == user_id)


# ── ค่าที่ยอมรับ ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["member", "manager", "admin"])
def test_the_three_real_roles_are_accepted(client, role):
    user = _make(client, f"ok-{role}", role)
    assert user["role"] == role


@pytest.mark.parametrize("bad", ["Admin", "adminn", "owner", "", "MANAGER "])
def test_a_role_the_system_does_not_use_is_refused(client, bad):
    """ค่าที่พิมพ์ผิดเคยบันทึกผ่าน แล้วคนคนนั้นกลายเป็น member เงียบ ๆ"""
    response = client.post(
        "/admin/users",
        headers=auth(client.admin_key),
        json={"external_id": f"bad-{bad or 'empty'}", "role": bad},
    )
    if response.status_code == 201:
        # ยอมรับได้เฉพาะกรณีที่ระบบ normalise ให้เป็นค่าจริงแล้วเท่านั้น
        assert response.json()["role"] in {"member", "manager", "admin"}
        assert response.json()["role"] == bad.strip().lower()
    else:
        assert response.status_code == 400
        assert "role" in response.json()["error"]["message"].lower()


@pytest.mark.parametrize("legacy", ["student", "instructor"])
def test_legacy_names_are_not_accepted_as_input(client, legacy):
    """`normalise_role` แปลชื่อเก่าตอน *อ่าน* ข้อมูลที่มีอยู่ · ทางเข้ายังรับเฉพาะชื่อปัจจุบัน
    เพื่อไม่ให้มีสองคำเรียกสิ่งเดียวกันงอกใหม่"""
    response = client.post(
        "/admin/users", headers=auth(client.admin_key),
        json={"external_id": f"legacy-{legacy}", "role": legacy},
    )
    assert response.status_code == 400


# ── เปลี่ยน role ทีหลัง ─────────────────────────────────────────────────────

def test_a_role_can_be_changed_after_the_account_exists(client):
    user = _make(client, "promote-me", "member")
    response = client.patch(
        f"/admin/users/{user['id']}", headers=auth(client.admin_key), json={"role": "manager"}
    )
    assert response.status_code == 200
    assert _role_of(client, user["id"]) == "manager"


def test_changing_to_a_role_that_does_not_exist_is_refused(client):
    user = _make(client, "typo-target", "member")
    response = client.patch(
        f"/admin/users/{user['id']}", headers=auth(client.admin_key), json={"role": "superuser"}
    )
    assert response.status_code == 400
    assert _role_of(client, user["id"]) == "member"


def test_the_only_administrator_cannot_step_down(client):
    """ไม่เหลือ admin = ไม่มีใครออก key ตั้ง quota หรือแก้ registry ได้ และกลับไม่ได้"""
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    admins = [u for u in users if u["role"] == "admin"]
    # ลดให้เหลือ admin คนเดียวก่อน — fixture สร้างมาสองคน
    for extra in admins[1:]:
        assert client.patch(f"/admin/users/{extra['id']}",
                            headers=auth(client.admin_key),
                            json={"role": "manager"}).status_code == 200
    last = admins[0]

    response = client.patch(
        f"/admin/users/{last['id']}", headers=auth(client.admin_key), json={"role": "member"}
    )
    assert response.status_code == 400
    assert "only administrator" in response.json()["error"]["message"].lower()
    assert _role_of(client, last["id"]) == "admin"


def test_stepping_down_works_once_somebody_else_is_admin(client):
    """กันไว้เพื่อไม่ให้ล็อกตัวเอง ไม่ใช่ห้ามเปลี่ยนตลอดไป"""
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    first = next(u for u in users if u["role"] == "admin")
    second = _make(client, "second-admin", "admin")
    # key ของ admin คนใหม่ · ต้องออกก่อนที่คนแรกจะลดตัวเอง เพราะหลังจากนั้น key
    # เดิมกลายเป็นของ manager ซึ่งเห็นเฉพาะคนในกลุ่มตัวเอง
    successor = client.post(
        "/admin/api-keys", headers=auth(client.admin_key),
        json={"user_id": second["id"], "name": "successor"},
    ).json()["api_key"]

    response = client.patch(
        f"/admin/users/{first['id']}", headers=auth(client.admin_key), json={"role": "manager"}
    )
    assert response.status_code == 200

    listed = client.get("/admin/users", headers=auth(successor)).json()["data"]
    roles = {u["id"]: u["role"] for u in listed}
    assert roles[first["id"]] == "manager"
    assert roles[second["id"]] == "admin"


def test_a_member_cannot_promote_themselves(client, member_key):
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    me = next(u for u in users if u["role"] == "member")
    response = client.patch(
        f"/admin/users/{me['id']}", headers=auth(member_key), json={"role": "admin"}
    )
    assert response.status_code in (401, 403)
    assert _role_of(client, me["id"]) == "member"
