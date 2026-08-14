"""Removing a workspace that is genuinely finished with.

There was no way to. A class created by mistake, or a pilot that ended, stayed
in the list forever — the only route was editing the database by hand, which is
what had to be done to clear test data off the live gateway.

Deleting is refused while anybody is still in it or any key is still pinned to
it: such a key permits nothing once its workspace is gone, and nothing on screen
would say why. Suspending is the reversible answer and the message says so.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def user(client, external_id):
    return client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id}).json()


def workspace(client, code="CS101"):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": code, "name": code}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": ["coding"]})
    return ws


def codes(client):
    return {w["code"] for w in
            client.get("/admin/workspaces", headers=auth(client.admin_key)).json()["data"]}


def remove(client, ws, key=None):
    return client.delete(f"/admin/workspaces/{ws['id']}",
                         headers=auth(key or client.admin_key))


def test_an_empty_workspace_can_be_removed(client):
    ws = workspace(client)
    assert remove(client, ws).status_code == 200
    assert "CS101" not in codes(client)


def test_its_model_list_goes_with_it(client, writable_config):
    ws = workspace(client)
    remove(client, ws)

    # สร้างใหม่รหัสเดิม ต้องไม่ได้โมเดลของเก่าติดมาด้วย
    again = workspace(client, "CS101")
    listed = client.get("/admin/workspaces", headers=auth(client.admin_key)).json()["data"]
    assert next(w for w in listed if w["id"] == again["id"])["id"] != ws["id"]


def test_a_workspace_with_members_is_kept(client):
    ws = workspace(client)
    student = user(client, "s1")
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": student["id"]})

    response = remove(client, ws)
    assert response.status_code == 400
    assert response.json()["error"]["details"]["members"] == 1
    assert "CS101" in codes(client)


def test_a_workspace_with_keys_issued_for_it_is_kept(client):
    """key ที่ผูกไว้จะใช้ไม่ได้ทันทีและไม่มีอะไรบอกว่าทำไม"""
    ws = workspace(client)
    student = user(client, "s2")
    client.post("/admin/api-keys", headers=auth(client.admin_key),
                json={"user_id": student["id"], "workspace_id": ws["id"], "name": "lab"})

    response = remove(client, ws)
    assert response.status_code == 400
    assert response.json()["error"]["details"]["pinned_keys"] == 1


def test_the_refusal_points_at_suspend(client):
    """ถ้าที่ต้องการคือ "หยุดใช้ไว้ก่อน" มีทางที่ย้อนกลับได้อยู่แล้ว"""
    ws = workspace(client)
    student = user(client, "s3")
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": student["id"]})

    assert "suspend" in remove(client, ws).json()["error"]["message"].lower()


def test_a_revoked_key_does_not_hold_it_open(client):
    """เพิกถอนแล้วไม่ใช่ key ที่ยังทำงาน จึงไม่ควรกันการลบไว้ตลอดกาล"""
    ws = workspace(client)
    student = user(client, "s4")
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": student["id"], "workspace_id": ws["id"],
                            "name": "old"}).json()
    client.delete(f"/admin/api-keys/{key['id']}", headers=auth(client.admin_key))

    assert remove(client, ws).status_code == 200


def test_a_manager_cannot_remove_a_workspace(client):
    """ระงับเป็นงานของ manager ได้ · ลบถาวรเป็นของ admin"""
    lecturer = user(client, "lecturer")
    ws = workspace(client)
    client.post("/admin/users/" + lecturer["id"], headers=auth(client.admin_key))
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": lecturer["id"]})
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": lecturer["id"], "name": "k"}).json()["api_key"]

    assert remove(client, ws, key).status_code == 403


def test_removing_one_that_is_not_there_says_so(client):
    assert client.delete("/admin/workspaces/not-a-workspace",
                         headers=auth(client.admin_key)).status_code == 400
