"""Naming a set of models once instead of ticking it into twenty classes.

Giving twenty courses the same four models meant ticking four boxes twenty
times, and adding a fifth model meant going back to all twenty. A bundle is a
way of *writing* that rule once. It is deliberately not a new kind of rule: what
it expands to is folded into the models the workspace already allows, and then
narrowed by everything that narrowed before.

The property that has to hold no matter what: a bundle can never let somebody
call a model they could not otherwise call. If it could, handing yourself a
bundle would be the way around every other check.
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


def group(client, name, models, **extra):
    return client.post("/admin/access-groups", headers=auth(client.admin_key),
                       json={"name": name, "models": list(models), **extra})


def workspace(client, code, models=(), groups=()):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": code, "name": code}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": list(models), "access_groups": list(groups)})
    return ws


def join(client, ws, person):
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": person["id"]})


def issue(client, person, **extra):
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": person["id"], "name": "k", **extra}).json()["api_key"]


def catalogue(client, key):
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


# ── the bundle itself ───────────────────────────────────────────────────────

def test_a_bundle_is_created_with_the_models_it_names(client):
    response = group(client, "coding-set", ["coding", "muse-local"])
    assert response.status_code == 201
    assert set(response.json()["models"]) == {"coding", "muse-local"}


def test_a_bundle_cannot_name_a_model_that_does_not_exist(client):
    response = group(client, "typo", ["coding", "not-a-model"])
    assert response.status_code == 404
    assert "not-a-model" in response.json()["error"]["message"]


def test_two_bundles_cannot_share_a_name(client):
    group(client, "coding-set", ["coding"])
    assert group(client, "coding-set", ["muse-local"]).status_code == 400


# ── what it does to permission ──────────────────────────────────────────────

def test_a_class_holding_a_bundle_can_call_what_it_names(client):
    bundle = group(client, "coding-set", ["coding", "muse-local"]).json()
    student = user(client, "s1")
    join(client, workspace(client, "CS101", groups=[bundle["id"]]), student)

    assert catalogue(client, issue(client, student)) == {"coding", "muse-local"}


def test_the_bundle_adds_to_what_was_ticked_directly(client):
    """สองทางตอบคำถามเดียวกัน — รวมกัน ไม่ใช่ทางใดทางหนึ่งชนะ"""
    bundle = group(client, "vision-set", ["gemma-vision"]).json()
    student = user(client, "s2")
    join(client, workspace(client, "CS101", models=["coding"], groups=[bundle["id"]]), student)

    assert catalogue(client, issue(client, student)) == {"coding", "gemma-vision"}


def test_editing_the_bundle_reaches_every_class_holding_it(client):
    """เหตุผลทั้งหมดที่มีมัด: แก้ที่เดียวถึงทุกวิชา"""
    bundle = group(client, "coding-set", ["coding"]).json()
    student = user(client, "s3")
    join(client, workspace(client, "CS101", groups=[bundle["id"]]), student)
    key = issue(client, student)
    assert catalogue(client, key) == {"coding"}

    response = client.patch(f"/admin/access-groups/{bundle['id']}",
                            headers=auth(client.admin_key),
                            json={"models": ["coding", "gemma-vision"]})
    assert response.status_code == 200
    assert response.json()["used_by"] == 1, "ต้องบอกว่ากระทบกี่วิชา"
    assert catalogue(client, key) == {"coding", "gemma-vision"}


def test_disabling_a_bundle_takes_its_models_away_everywhere(client):
    bundle = group(client, "coding-set", ["coding"]).json()
    student = user(client, "s4")
    join(client, workspace(client, "CS101", groups=[bundle["id"]]), student)
    key = issue(client, student)

    client.patch(f"/admin/access-groups/{bundle['id']}",
                 headers=auth(client.admin_key), json={"enabled": False})
    assert catalogue(client, key) == set()


def test_a_bundle_on_a_key_narrows_and_never_widens(client):
    """คุณสมบัติที่ต้องจริงเสมอ · ไม่งั้นการแจกมัดให้ตัวเองคือทางลัดข้ามทุกกฎ"""
    wide = group(client, "everything", ["coding", "gemma-vision", "muse-local"]).json()
    student = user(client, "s5")
    join(client, workspace(client, "CS101", models=["coding"]), student)

    key = issue(client, student, access_groups=[wide["id"]])
    assert catalogue(client, key) == {"coding"}, "มัดบน key ขยายสิทธิ์ของกลุ่มไม่ได้"


def test_a_bundle_and_a_list_on_the_same_key_add_up(client):
    """ทั้งคู่ตอบว่า "key ใบนี้ออกมาเพื่ออะไร" จึงรวมกันก่อน แล้วค่อยไปตัดกับกลุ่ม"""
    bundle = group(client, "vision-set", ["gemma-vision"]).json()
    student = user(client, "s6")
    join(client, workspace(client, "CS101",
                           models=["coding", "gemma-vision", "muse-local"]), student)

    key = issue(client, student, models=["coding"], access_groups=[bundle["id"]])
    assert catalogue(client, key) == {"coding", "gemma-vision"}


def test_a_suspended_class_still_grants_nothing_through_a_bundle(client):
    """ระงับวิชาต้องตัดทุกทาง ไม่ใช่ตัดเฉพาะช่องที่ติ๊กไว้"""
    bundle = group(client, "coding-set", ["coding"]).json()
    student = user(client, "s7")
    ws = workspace(client, "CS101", groups=[bundle["id"]])
    join(client, ws, student)
    key = issue(client, student)

    client.patch(f"/admin/workspaces/{ws['id']}/status",
                 headers=auth(client.admin_key), json={"status": "suspended"})
    assert catalogue(client, key) == set()


# ── who may hand one out ────────────────────────────────────────────────────

def test_a_manager_cannot_invent_a_bundle(client):
    """สร้างมัดเองได้ = แจกโมเดลอะไรให้ตัวเองก็ได้ · เป็น admin เท่านั้น"""
    lecturer = user(client, "lecturer", role="manager")
    ws = workspace(client, "CS101", models=["coding"])
    join(client, ws, lecturer)

    response = client.post("/admin/access-groups", headers=auth(issue(client, lecturer)),
                           json={"name": "mine", "models": ["muse-local"]})
    assert response.status_code == 403


def test_a_manager_cannot_hand_out_a_bundle_beyond_their_own_reach(client):
    lecturer = user(client, "lecturer2", role="manager")
    ws = workspace(client, "CS101", models=["coding"])
    join(client, ws, lecturer)
    bundle = group(client, "wide", ["coding", "muse-local"]).json()

    response = client.post(f"/admin/workspaces/{ws['id']}/models",
                           headers=auth(issue(client, lecturer)),
                           json={"models": ["coding"], "access_groups": [bundle["id"]]})
    assert response.status_code == 403
    assert "muse-local" in response.json()["error"]["message"]


# ── housekeeping ────────────────────────────────────────────────────────────

def test_a_bundle_in_use_is_not_deleted_by_accident(client):
    bundle = group(client, "coding-set", ["coding"]).json()
    workspace(client, "CS101", groups=[bundle["id"]])

    response = client.delete(f"/admin/access-groups/{bundle['id']}",
                             headers=auth(client.admin_key))
    assert response.status_code == 400
    assert "disable" in response.json()["error"]["message"].lower()


def test_an_unused_bundle_can_be_deleted(client):
    bundle = group(client, "unused", ["coding"]).json()
    assert client.delete(f"/admin/access-groups/{bundle['id']}",
                         headers=auth(client.admin_key)).status_code == 200


def test_the_listing_says_how_many_classes_hold_each_bundle(client):
    bundle = group(client, "coding-set", ["coding"]).json()
    workspace(client, "CS101", groups=[bundle["id"]])
    workspace(client, "CS102", groups=[bundle["id"]])

    listed = client.get("/admin/access-groups", headers=auth(client.admin_key)).json()["data"]
    assert next(g for g in listed if g["id"] == bundle["id"])["used_by"] == 2


def test_not_mentioning_bundles_leaves_them_alone(client):
    """ไคลเอนต์รุ่นก่อนที่ยังไม่รู้จักมัด ต้องไม่ลบมัดทิ้งเพราะไม่ได้พูดถึง"""
    bundle = group(client, "coding-set", ["coding"]).json()
    ws = workspace(client, "CS101", groups=[bundle["id"]])

    client.post(f"/admin/workspaces/{ws['id']}/models",
                headers=auth(client.admin_key), json={"models": ["gemma-vision"]})

    spaces = client.get("/admin/workspaces", headers=auth(client.admin_key)).json()["data"]
    still = next(w for w in spaces if w["id"] == ws["id"])
    assert still["access_groups"] == [bundle["id"]]
