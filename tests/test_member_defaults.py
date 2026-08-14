"""What a key starts as, so enrolling thirty people is not thirty decisions.

Issuing keys for a class meant typing the same models and the same expiry
thirty times, and the one that got mistyped surfaced weeks later as a student
who could not call the model everybody else could. The class carries the
answer; the key issue picks it up.

Only blanks are filled. An explicitly empty list is a decision — "this key is
not restricted" — and overwriting it would be the console arguing with the
person using it. And what was filled in comes back in the response, because a
default that applies silently is a setting nobody knows they have.
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


def workspace(client, code, **settings):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": code, "name": code}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": ["coding", "gemma-vision"], **settings})
    return ws


def join(client, ws, person):
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                json={"user_id": person["id"]})


def issue(client, person, **extra):
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": person["id"], "name": "k", **extra}).json()


def test_a_new_key_starts_from_the_class_defaults(client):
    student = user(client, "s1")
    ws = workspace(client, "CS101", default_member_models=["coding"], default_key_days=90)
    join(client, ws, student)

    key = issue(client, student)
    assert key["models"] == ["coding"]
    assert key["expires_at"] is not None
    assert key["applied_defaults"]["from_workspace"] == "CS101"


def test_the_class_expiry_wins_over_the_schema_default(client):
    """`expires_in_days` มีค่า default ของตัวเอง · อ่านค่านั้นว่า "ผู้เรียกเลือกแล้ว"
    ทำให้ค่าของวิชาแพ้เงียบ ๆ — ต้องดูที่ว่าผู้เรียกส่งฟิลด์มาไหม ไม่ใช่ดูที่ค่า"""
    from datetime import UTC, datetime

    student = user(client, "s1b")
    join(client, workspace(client, "CS101", default_key_days=45), student)

    key = issue(client, student)
    assert key["applied_defaults"]["expires_in_days"] == 45
    days = (datetime.fromisoformat(key["expires_at"]) - datetime.now(UTC)).days
    assert 43 <= days <= 45, f"ควรหมดอายุใน 45 วัน ไม่ใช่ {days}"


def test_what_was_filled_in_is_reported(client):
    """ค่าเริ่มต้นที่เงียบ ๆ คือค่าที่ไม่มีใครรู้ว่าตัวเองมี"""
    student = user(client, "s2")
    join(client, workspace(client, "CS101", default_member_models=["coding"]), student)

    applied = issue(client, student)["applied_defaults"]
    assert applied["models"] == ["coding"]
    assert applied["from_workspace"] == "CS101"


def test_an_explicit_choice_is_never_overwritten(client):
    student = user(client, "s3")
    join(client, workspace(client, "CS101", default_member_models=["coding"]), student)

    key = issue(client, student, models=["gemma-vision"])
    assert key["models"] == ["gemma-vision"]
    assert "models" not in key["applied_defaults"]


def test_an_explicitly_empty_list_is_a_decision(client):
    """ส่ง [] มาแปลว่า "ไม่จำกัด" ไม่ใช่ "ยังไม่ได้เลือก\""""
    student = user(client, "s4")
    join(client, workspace(client, "CS101", default_member_models=["coding"]), student)

    key = issue(client, student, models=[])
    assert key["models"] == []


def test_bundles_can_be_the_default_too(client):
    bundle = client.post("/admin/access-groups", headers=auth(client.admin_key),
                         json={"name": "set", "models": ["coding"]}).json()
    student = user(client, "s5")
    join(client, workspace(client, "CS101", default_access_groups=[bundle["id"]]), student)

    key = issue(client, student)
    assert key["access_groups"] == [bundle["id"]]


def test_a_key_pinned_to_a_class_uses_that_class_defaults(client):
    student = user(client, "s6")
    mine = workspace(client, "CS101", default_member_models=["coding"])
    theirs = workspace(client, "ART200", default_member_models=["gemma-vision"])
    join(client, mine, student)

    key = issue(client, student, workspace_id=theirs["id"])
    assert key["models"] == ["gemma-vision"], "ผูกกับวิชาไหน ใช้ค่าของวิชานั้น"


def test_two_classes_means_no_guess(client):
    """สองวิชาไม่มีคำตอบที่ถูก · เดาแล้วผิดคือแจกค่าของเทอมอื่นให้เขา"""
    student = user(client, "s7")
    join(client, workspace(client, "CS101", default_member_models=["coding"]), student)
    join(client, workspace(client, "ART200", default_member_models=["gemma-vision"]), student)

    key = issue(client, student)
    assert key["applied_defaults"] == {}
    assert key["models"] == []


def test_someone_in_no_class_gets_no_defaults(client):
    key = issue(client, user(client, "s8"))
    assert key["applied_defaults"] == {}


def test_defaults_survive_a_save_that_does_not_mention_them(client):
    """ไคลเอนต์รุ่นก่อนที่ยังไม่รู้จักค่าเริ่มต้น ต้องไม่ล้างมันทิ้ง"""
    ws = workspace(client, "CS101", default_member_models=["coding"], default_key_days=30)
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": ["coding"]})

    spaces = client.get("/admin/workspaces", headers=auth(client.admin_key)).json()["data"]
    still = next(w for w in spaces if w["id"] == ws["id"])
    assert still["default_member_models"] == ["coding"]
    assert still["default_key_days"] == 30


def test_a_default_naming_a_model_that_does_not_exist_is_refused(client):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "CS101", "name": "CS101"}).json()
    response = client.post(f"/admin/workspaces/{ws['id']}/models",
                           headers=auth(client.admin_key),
                           json={"models": [], "default_member_models": ["not-a-model"]})
    assert response.status_code == 404


# ── key kinds ───────────────────────────────────────────────────────────────

def test_a_key_is_a_persons_key_unless_it_says_otherwise(client):
    assert issue(client, user(client, "s9"))["kind"] == "person"


def test_a_service_key_says_so(client):
    """key ของ CI ที่ปนอยู่กับ key นักศึกษาคือสิ่งที่ทำให้การไล่ตรวจกินเวลาทั้งบ่าย"""
    key = issue(client, user(client, "ci"), kind="service")
    assert key["kind"] == "service"

    listed = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    assert next(k for k in listed if k["id"] == key["id"])["kind"] == "service"


def test_an_unknown_kind_falls_back_to_person(client):
    """ชนิดที่พิมพ์ผิดต้องไม่กลายเป็นชนิดใหม่ที่ไม่มีใครกรองเจอ"""
    assert issue(client, user(client, "typo"), kind="robot")["kind"] == "person"
