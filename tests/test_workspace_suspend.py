"""Putting a class on hold without destroying anybody's credentials.

`Workspace.status` shipped in the first release with a default of "active" and
nothing ever read it — the same shape of problem as membership before v1.5: a
field that looks like a switch and is wired to nothing. Until now the only way
to stop a class using the gateway was to revoke every key in it, which cannot
be undone and punishes people for the term ending.

The rule adds nothing new. A suspended workspace is a workspace that grants no
models, and every existing rule follows from that.
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


def workspace(client, code, models):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": code, "name": code}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": list(models)})
    return ws


def join(client, ws, person):
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": person["id"]})


def issue(client, person, **extra):
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": person["id"], "name": "k", **extra}).json()["api_key"]


def catalogue(client, key):
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def set_status(client, ws, status):
    return client.patch(f"/admin/workspaces/{ws['id']}/status",
                        headers=auth(client.admin_key), json={"status": status})


def test_suspending_a_class_stops_its_models(client):
    student = user(client, "s1")
    ws = workspace(client, "CS101", ["coding"])
    join(client, ws, student)
    key = issue(client, student)
    assert catalogue(client, key) == {"coding"}

    response = set_status(client, ws, "suspended")
    assert response.status_code == 200
    assert response.json()["members_affected"] == 1
    assert catalogue(client, key) == set()


def test_it_can_be_undone_which_is_the_whole_point(client):
    student = user(client, "s2")
    ws = workspace(client, "CS101", ["coding"])
    join(client, ws, student)
    key = issue(client, student)

    set_status(client, ws, "suspended")
    set_status(client, ws, "active")
    assert catalogue(client, key) == {"coding"}


def test_the_keys_are_left_alone(client):
    """เพิกถอน key คือทำลายของที่กู้คืนไม่ได้ · ระงับต้องไม่แตะมัน"""
    student = user(client, "s3")
    ws = workspace(client, "CS101", ["coding"])
    join(client, ws, student)
    issue(client, student)

    set_status(client, ws, "suspended")

    keys = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    assert all(k["revoked"] is False for k in keys if k["user_id"] == student["id"])


def test_somebody_in_another_class_keeps_working(client):
    """ระงับวิชาหนึ่งต้องไม่ลามไปวิชาอื่นของคนเดียวกัน"""
    student = user(client, "s4")
    cs101 = workspace(client, "CS101", ["coding"])
    art200 = workspace(client, "ART200", ["gemma-vision"])
    join(client, cs101, student)
    join(client, art200, student)
    key = issue(client, student)
    assert catalogue(client, key) == {"coding", "gemma-vision"}

    set_status(client, cs101, "suspended")
    assert catalogue(client, key) == {"gemma-vision"}


def test_a_key_pinned_to_a_suspended_class_can_call_nothing(client):
    """ผูกไว้กับวิชานั้นโดยเฉพาะ · วิชาหยุด key ก็หยุด ซึ่งตรงกับความหมายของการระงับ"""
    student = user(client, "s5")
    ws = workspace(client, "CS101", ["coding"])
    join(client, ws, student)
    key = issue(client, student, workspace_id=ws["id"])
    assert catalogue(client, key) == {"coding"}

    set_status(client, ws, "suspended")
    assert catalogue(client, key) == set()


def test_a_status_that_is_not_a_status_is_refused(client):
    ws = workspace(client, "CS101", ["coding"])
    response = set_status(client, ws, "paused-ish")
    assert response.status_code == 400
    assert "active" in response.json()["error"]["message"]


def test_a_manager_cannot_suspend_a_class_they_do_not_run(client):
    lecturer = user(client, "lecturer", role="manager")
    mine = workspace(client, "CS101", ["coding"])
    theirs = workspace(client, "ART200", ["gemma-vision"])
    join(client, mine, lecturer)
    key = issue(client, lecturer)

    denied = client.patch(f"/admin/workspaces/{theirs['id']}/status",
                          headers=auth(key), json={"status": "suspended"})
    assert denied.status_code == 403

    allowed = client.patch(f"/admin/workspaces/{mine['id']}/status",
                           headers=auth(key), json={"status": "suspended"})
    assert allowed.status_code == 200
