"""Putting people into a workspace and taking them out again.

`POST /admin/workspaces/{id}/join` shipped from the start and no page ever
called it: a workspace could be created and nobody put in one. There was also
no way out — the wrong person added stayed added, and last term's students
stayed on the list forever — and nothing reported who was in what, so the
console could not have shown it even if it wanted to.
"""

from __future__ import annotations


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _user(client, external_id):
    r = client.post("/admin/users", headers=auth(client.admin_key),
                    json={"external_id": external_id, "display_name": external_id})
    assert r.status_code == 201, r.text
    return r.json()


def _workspace(client, code):
    r = client.post("/admin/workspaces", headers=auth(client.admin_key),
                    json={"code": code, "name": f"{code} class"})
    assert r.status_code == 201, r.text
    return r.json()


def _workspaces_of(client, user_id) -> list[str]:
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    return next(u.get("workspaces", []) for u in users if u["id"] == user_id)


def test_the_listing_says_which_workspaces_someone_is_in(client):
    """เดิมไม่มีทางรู้เลย — หน้าเว็บจึงแสดงไม่ได้แม้อยากแสดง"""
    user = _user(client, "6412001")
    assert _workspaces_of(client, user["id"]) == []

    ws = _workspace(client, "CS101")
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": user["id"]})
    assert _workspaces_of(client, user["id"]) == ["CS101"]


def test_someone_can_be_in_several(client):
    user = _user(client, "6412002")
    for code in ("CS101", "MA201"):
        ws = _workspace(client, code)
        client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                    json={"user_id": user["id"]})
    assert _workspaces_of(client, user["id"]) == ["CS101", "MA201"]


def test_joining_twice_is_not_an_error(client):
    """เผลอกดสองครั้งไม่ควรพัง และไม่ควรได้ membership ซ้ำ"""
    user = _user(client, "6412003")
    ws = _workspace(client, "CS101")
    first = client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                        json={"user_id": user["id"]})
    second = client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                         json={"user_id": user["id"]})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["status"] == "already_joined"
    assert _workspaces_of(client, user["id"]) == ["CS101"]


def test_somebody_can_be_taken_out(client):
    user = _user(client, "6412004")
    ws = _workspace(client, "CS101")
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": user["id"]})

    response = client.delete(f"/admin/workspaces/{ws['id']}/members/{user['id']}",
                             headers=auth(client.admin_key))
    assert response.status_code == 200
    assert response.json()["status"] == "removed"
    assert _workspaces_of(client, user["id"]) == []


def test_removing_someone_who_was_never_in_says_so(client):
    user = _user(client, "6412005")
    ws = _workspace(client, "CS101")
    response = client.delete(f"/admin/workspaces/{ws['id']}/members/{user['id']}",
                             headers=auth(client.admin_key))
    assert response.status_code == 400
    assert "not in this workspace" in response.json()["error"]["message"].lower()


def test_removal_leaves_the_key_alone(client):
    """เพิกถอน key ให้ด้วยคือตัดสินใจแทนผู้ใช้ในเรื่องที่กู้คืนไม่ได้"""
    user = _user(client, "6412006")
    ws = _workspace(client, "CS101")
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": user["id"]})
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": user["id"], "workspace_id": ws["id"], "name": "lab"}).json()

    client.delete(f"/admin/workspaces/{ws['id']}/members/{user['id']}",
                  headers=auth(client.admin_key))

    keys = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    still = next(k for k in keys if k["id"] == key["id"])
    assert still["revoked"] is False


def test_membership_now_decides_which_models_you_may_call(client, writable_config):
    """v1.5: อยู่กลุ่มไหน ใช้ได้เท่าที่กลุ่มนั้นเปิดไว้

    ก่อนหน้านี้ membership เป็นแค่บันทึกทางบัญชี — จดว่าใครอยู่วิชาไหนแล้วไม่มีผล
    อะไรเลย ซึ่งไม่ตรงกับที่ทุกคนเข้าใจตอนกด "เพิ่มเข้ากลุ่ม"
    """
    user = _user(client, "6412008")
    ws = _workspace(client, "CS101")
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
               json={"models": ["coding"]})
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": user["id"]})

    # key ที่ไม่ผูก workspace — สิทธิ์จึงต้องมาจากกลุ่มของเจ้าของ
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": user["id"], "name": "laptop"}).json()["api_key"]

    catalogue = {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}
    assert catalogue == {"coding"}, "ต้องเหลือเท่าที่กลุ่มเปิดให้"


def test_someone_in_no_group_at_all_keeps_everything(client, writable_config):
    """ไม่มีกลุ่ม = ไม่มีอะไรมาจำกัด · ถ้าตีเป็น "ห้ามหมด" คือล็อกคนออกยกชุด

    ระบบที่ยังไม่เริ่มใช้ workspace เลยต้องทำงานได้เหมือนเดิมทุกอย่าง
    """
    user = _user(client, "6412009")
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": user["id"], "name": "laptop"}).json()["api_key"]

    catalogue = {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}
    assert len(catalogue) > 1


def test_a_member_cannot_add_themselves_to_a_workspace(client, member_key):
    user = _user(client, "6412007")
    ws = _workspace(client, "CS101")
    response = client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(member_key),
                           json={"user_id": user["id"]})
    assert response.status_code in (401, 403)
    assert _workspaces_of(client, user["id"]) == []
