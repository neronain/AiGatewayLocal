"""One place decides which models a caller may use, and it only ever narrows.

Three things can restrict a key, and until v1.5 only two of them were wired up:
the workspace the key was issued for, and the alias list on the key itself.
Membership — the record of who is in which class — granted nothing at all,
which is not what anybody assumes when they press "add to group".

Turning it on re-permissions keys already in circulation, so these cover both
sides of the switch, and the rule that keeps it safe: a person in no group at
all is unrestricted, because reading "no groups" as "nothing allowed" would
lock out every key issued before workspaces were used.

Managers are scoped like members. Somebody who looks after CS101 has no business
handing out ART200's models. Admins are not scoped: they run the gateway, and
the alternative is adding them to every workspace forever.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def make_user(client, external_id, role="member"):
    return client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id, "role": role}).json()


def make_workspace(client, code, models):
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": code, "name": code}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": models})
    return ws


def join(client, ws, user):
    return client.post(f"/admin/workspaces/{ws['id']}/join",
                       headers=auth(client.admin_key), json={"user_id": user["id"]})


def issue(client, user, **extra):
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": user["id"], "name": "k", **extra}).json()["api_key"]


def catalogue(client, key):
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def test_a_member_sees_what_their_group_allows(client):
    user = make_user(client, "s1")
    join(client, make_workspace(client, "CS101", ["coding"]), user)
    assert catalogue(client, issue(client, user)) == {"coding"}


def test_two_groups_add_up(client):
    """เพิ่มเข้าอีกกลุ่มต้องได้เพิ่ม ไม่ใช่เสีย — intersection จะทำตรงกันข้ามกับคำว่า "เพิ่ม\""""
    user = make_user(client, "s2")
    join(client, make_workspace(client, "CS101", ["coding"]), user)
    join(client, make_workspace(client, "ART200", ["gemma-vision"]), user)
    assert catalogue(client, issue(client, user)) == {"coding", "gemma-vision"}


def test_a_manager_is_scoped_to_the_groups_they_look_after(client):
    """เจ้าของระบบตัดสินไว้: manager ถูกจำกัดตามกลุ่มที่ตัวเองดูแล"""
    boss = make_user(client, "lecturer", role="manager")
    join(client, make_workspace(client, "CS101", ["coding"]), boss)
    assert catalogue(client, issue(client, boss)) == {"coding"}


def test_an_admin_is_not_scoped(client):
    """admin ดูแล gateway ทั้งตัว · การบังคับให้ใส่เข้าทุกกลุ่มคืองานที่ไม่มีวันจบ"""
    boss = make_user(client, "root", role="admin")
    join(client, make_workspace(client, "CS101", ["coding"]), boss)
    assert catalogue(client, issue(client, boss)) > {"coding"}


def test_the_key_binding_wins_over_the_owners_groups(client):
    """ผูก workspace ไว้ตอนออก key = เจตนาชัดเจน ห้ามให้กลุ่มของเจ้าของมาแทนที่"""
    user = make_user(client, "s3")
    join(client, make_workspace(client, "CS101", ["coding"]), user)
    art = make_workspace(client, "ART200", ["gemma-vision"])
    key = issue(client, user, workspace_id=art["id"])
    assert catalogue(client, key) == {"gemma-vision"}


def test_the_list_on_the_key_narrows_further(client):
    user = make_user(client, "s4")
    join(client, make_workspace(client, "CS101", ["coding", "gemma-vision"]), user)
    assert catalogue(client, issue(client, user, models=["coding"])) == {"coding"}


def test_the_list_on_the_key_cannot_widen(client):
    """key ระบุโมเดลที่กลุ่มไม่ได้เปิดไว้ ต้องไม่ได้สิทธิ์เพิ่ม"""
    user = make_user(client, "s5")
    join(client, make_workspace(client, "CS101", ["coding"]), user)
    assert catalogue(client, issue(client, user, models=["coding", "gemma-vision"])) == {"coding"}


def test_calling_something_outside_the_scope_says_why(client):
    user = make_user(client, "s6")
    join(client, make_workspace(client, "CS101", ["coding"]), user)
    response = client.post(
        "/v1/chat/completions",
        headers=auth(issue(client, user)),
        json={"model": "gemma-vision", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 403
    message = response.json()["error"]["message"]
    assert "workspaces you belong to" in message, "ต้องบอกว่าถูกจำกัดด้วยกติกาไหน"
    assert "coding" in message, "และบอกว่าเรียกอะไรได้บ้าง"


def test_a_group_with_no_models_allows_nothing_and_the_join_says_so(client):
    """ความเสี่ยงที่ PRD เตือนไว้: ใส่คนเข้ากลุ่มว่าง = ตัดสิทธิ์เขา ไม่ใช่ให้สิทธิ์"""
    user = make_user(client, "s7")
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "EMPTY", "name": "EMPTY"}).json()

    response = join(client, ws, user)
    assert "no models enabled" in response.json()["warning"]
    assert catalogue(client, issue(client, user)) == set()


def test_the_switch_can_be_turned_off_for_a_site_with_live_keys(writable_config):
    """FR-43 · ทางถอย

    เปิดสวิตช์นี้คือเปลี่ยนสิทธิ์ของ key ที่แจกออกไปแล้ว · ระบบที่มีคนใช้อยู่จริงต้อง
    ปิดไว้ก่อนได้ ดูผลจาก scripts/access_change_report.py แล้วค่อยเปิด
    """
    import yaml
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import _bootstrap_key

    path = writable_config / "gateway.yaml"
    gateway = yaml.safe_load(path.read_text())
    gateway["membership_grants_models"] = False
    path.write_text(yaml.safe_dump(gateway, sort_keys=False, allow_unicode=True))

    app = create_app()
    with TestClient(app) as client:
        client.admin_key = _bootstrap_key(app)
        user = make_user(client, "s9")
        join(client, make_workspace(client, "CS101", ["coding"]), user)
        assert catalogue(client, issue(client, user)) > {"coding"}, "ปิดแล้วต้องเป็นพฤติกรรมเดิม"


def test_the_catalogue_and_the_gate_never_disagree(client):
    """ลิสต์ที่โชว์ต้องเรียกได้จริงทุกตัว · โชว์ตัวที่เรียกไม่ได้คือหลอกให้เสียเวลาพิมพ์"""
    user = make_user(client, "s8")
    join(client, make_workspace(client, "CS101", ["coding"]), user)
    key = issue(client, user)

    listed = catalogue(client, key)
    for alias in ("coding", "gemma-vision", "muse-local"):
        refused = client.post(
            "/v1/chat/completions", headers=auth(key),
            json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
        ).status_code == 403
        assert refused != (alias in listed), f"{alias}: ลิสต์กับด่านตรวจไม่ตรงกัน"
