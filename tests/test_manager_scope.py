"""A manager runs their own classes, not the institution.

`require_manager` was a single yes/no: pass it and you saw every user, every
key, and the usage of the whole gateway. For a role meant to be the lecturer of
a course that is far too much - and worse, it was a way around the model rules,
because a manager could enable any model for a workspace they were in and had
just granted it to themselves.

Every route a manager can reach is now scoped to the workspaces they belong to.
The default runs the opposite way from model access on purpose: a manager in no
workspace manages nothing, because promoting somebody should not hand them the
institution while nobody is looking.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def user(client, external_id, role="member"):
    return client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id, "role": role}).json()


def workspace(client, code, models=("coding",)):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": code, "name": code}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": list(models)})
    return ws


def join(client, ws, person):
    return client.post(f"/admin/workspaces/{ws['id']}/join",
                       headers=auth(client.admin_key), json={"user_id": person["id"]})


def key_for(client, person, **extra):
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": person["id"], "name": "k", **extra}).json()["api_key"]


@pytest.fixture
def two_classes(client):
    """A lecturer for CS101, a student in each of two classes."""
    lecturer = user(client, "lecturer", role="manager")
    cs101 = workspace(client, "CS101", ["coding"])
    art200 = workspace(client, "ART200", ["gemma-vision"])
    mine = user(client, "student-cs")
    theirs = user(client, "student-art")

    join(client, cs101, lecturer)
    join(client, cs101, mine)
    join(client, art200, theirs)
    return {
        "key": key_for(client, lecturer),
        "lecturer": lecturer, "cs101": cs101, "art200": art200,
        "mine": mine, "theirs": theirs,
    }


def test_they_see_the_people_in_their_own_class(client, two_classes):
    listed = client.get("/admin/users", headers=auth(two_classes["key"])).json()["data"]
    ids = {u["id"] for u in listed}
    assert two_classes["mine"]["id"] in ids
    assert two_classes["theirs"]["id"] not in ids, "คนของวิชาอื่นต้องไม่โผล่"
    assert two_classes["lecturer"]["id"] in ids, "ต้องเห็นตัวเองเสมอ"


def test_they_see_only_their_own_workspaces(client, two_classes):
    listed = client.get("/admin/workspaces", headers=auth(two_classes["key"])).json()["data"]
    assert {w["code"] for w in listed} == {"CS101"}


def test_they_cannot_add_someone_to_a_class_they_do_not_run(client, two_classes):
    response = client.post(
        f"/admin/workspaces/{two_classes['art200']['id']}/join",
        headers=auth(two_classes["key"]), json={"user_id": two_classes["mine"]["id"]},
    )
    assert response.status_code == 403
    assert "workspaces you belong to" in response.json()["error"]["message"]


def test_they_cannot_set_the_models_of_a_class_they_do_not_run(client, two_classes):
    response = client.post(
        f"/admin/workspaces/{two_classes['art200']['id']}/models",
        headers=auth(two_classes["key"]), json={"models": ["coding"]},
    )
    assert response.status_code == 403


def test_they_cannot_grant_a_model_they_cannot_use(client, two_classes):
    """ไม่งั้นการจำกัดสิทธิ์เป็นแค่ฉากหน้า — เปิดโมเดลให้กลุ่มตัวเอง = ให้ตัวเอง"""
    response = client.post(
        f"/admin/workspaces/{two_classes['cs101']['id']}/models",
        headers=auth(two_classes["key"]), json={"models": ["coding", "muse-local"]},
    )
    assert response.status_code == 403
    assert "muse-local" in response.json()["error"]["message"]


def test_they_can_still_run_their_own_class(client, two_classes):
    """จำกัดแล้วต้องยังทำงานของตัวเองได้ ไม่ใช่ทำอะไรไม่ได้เลย"""
    newcomer = user(client, "late-enroller")
    joined = client.post(
        f"/admin/workspaces/{two_classes['cs101']['id']}/join",
        headers=auth(two_classes["key"]), json={"user_id": newcomer["id"]},
    )
    assert joined.status_code == 200

    issued = client.post("/admin/api-keys", headers=auth(two_classes["key"]),
                         json={"user_id": newcomer["id"], "name": "laptop"})
    assert issued.status_code == 201


def test_they_cannot_issue_a_key_to_someone_elses_student(client, two_classes):
    response = client.post("/admin/api-keys", headers=auth(two_classes["key"]),
                           json={"user_id": two_classes["theirs"]["id"], "name": "sneaky"})
    assert response.status_code == 403
    assert "your own workspaces" in response.json()["error"]["message"]


def test_they_cannot_issue_a_key_naming_a_model_they_cannot_use(client, two_classes):
    response = client.post(
        "/admin/api-keys", headers=auth(two_classes["key"]),
        json={"user_id": two_classes["mine"]["id"], "models": ["muse-local"]},
    )
    assert response.status_code == 403


def test_they_see_only_their_students_keys(client, two_classes):
    key_for(client, two_classes["theirs"], name="other-class")
    listed = client.get("/admin/api-keys", headers=auth(two_classes["key"])).json()["data"]
    owners = {k["user_id"] for k in listed}
    assert two_classes["theirs"]["id"] not in owners


def test_they_cannot_revoke_a_key_outside_their_class(client, two_classes):
    key_for(client, two_classes["theirs"])
    keys = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    target = next(k for k in keys if k["user_id"] == two_classes["theirs"]["id"])

    response = client.delete(f"/admin/api-keys/{target['id']}",
                             headers=auth(two_classes["key"]))
    assert response.status_code == 400
    # บอกว่า "ไม่พบ" เหมือนกับ id ที่ไม่มีจริง — ไม่ควรใช้ endpoint นี้ไล่เดาว่ามี key อะไรอยู่
    assert "not found" in response.json()["error"]["message"].lower()

    after = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    assert next(k for k in after if k["id"] == target["id"])["revoked"] is False


def test_they_cannot_read_test_results_for_a_model_outside_their_scope(client, two_classes):
    ok = client.get("/admin/models/coding/compatibility", headers=auth(two_classes["key"]))
    assert ok.status_code == 200

    hidden = client.get("/admin/models/muse-local/compatibility",
                        headers=auth(two_classes["key"]))
    assert hidden.status_code == 404


def test_usage_reporting_stops_at_their_own_class(client, two_classes):
    summary = client.get("/admin/usage/summary?days=7", headers=auth(two_classes["key"]))
    assert summary.status_code == 200

    denied = client.get(
        f"/admin/usage/summary?days=7&workspace_id={two_classes['art200']['id']}",
        headers=auth(two_classes["key"]),
    )
    assert denied.status_code == 403


def test_a_manager_in_no_workspace_manages_nothing_and_is_told_why(client):
    """ต่างจากสิทธิ์เรียกโมเดลโดยตั้งใจ · เลื่อนใครเป็น manager ต้องไม่เท่ากับยกทั้งสถาบันให้"""
    lonely = user(client, "unassigned", role="manager")
    lonely_key = key_for(client, lonely)
    someone = user(client, "not-theirs")

    listed = client.get("/admin/users", headers=auth(lonely_key)).json()["data"]
    assert {u["id"] for u in listed} == {lonely["id"]}

    response = client.post("/admin/api-keys", headers=auth(lonely_key),
                           json={"user_id": someone["id"], "name": "nope"})
    assert response.status_code == 403


def test_an_admin_is_scoped_to_nothing(client, two_classes):
    """คนดูแล gateway ต้องเห็นทั้งระบบ ไม่งั้นไม่มีใครดูแลได้เลย"""
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    ids = {u["id"] for u in users}
    assert {two_classes["mine"]["id"], two_classes["theirs"]["id"]} <= ids

    spaces = client.get("/admin/workspaces", headers=auth(client.admin_key)).json()["data"]
    assert {"CS101", "ART200"} <= {w["code"] for w in spaces}
