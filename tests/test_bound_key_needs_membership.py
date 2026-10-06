"""key ที่ออกให้ภายใต้ workspace ใช้ได้เฉพาะตอนเจ้าของยังเป็นสมาชิกของ workspace นั้น

เคสจริง (ตรวจ 2026-10-06 บน a5e9295): นักศึกษาคนหนึ่งอยู่ CS101 และถือ key ที่ออกให้
ภายใต้ CS101 · ผู้ดูแลเอาเขาออกจาก CS101 — คำตอบคือ 200 "removed" ชื่อหายจากรายชื่อ —
แล้ว key ใบเดิมยังเรียก `coding` ได้ 200 ต่อไป และ `/v1/models` ยังโชว์โมเดลของ CS101
ครบ · docstring ของ endpoint เขียนไว้เองว่า "key หยุดเองเมื่อสิทธิ์หายไป" ซึ่งไม่จริง:
โค้ดอ่านแค่ว่า key ผูก workspace ไหน ไม่เคยดูว่าเจ้าของยังอยู่ในนั้นไหม

เทสเดิม (`test_removal_leaves_the_key_alone`) ยืนยันแค่ `revoked is False` ซึ่งถูก แต่
ไม่ได้ถามคำถามที่สำคัญ: ใบนั้นยัง *ใช้ได้* ไหม

กติกาตอนนี้: ตรวจการเป็นสมาชิกทุกคำขอ ทุก surface · ไม่เพิกถอน key (กู้คืนไม่ได้) —
ใส่คนกลับเมื่อไร ใบเดิมกลับมาใช้ได้เอง · และคำตอบของการเอาออกบอกเองว่าใบไหนหยุด

อีกด้านหนึ่ง: คนที่ถูกเอาออกจาก workspace **สุดท้าย** กลายเป็น "ไม่อยู่กลุ่มไหน" ซึ่ง
โดยการออกแบบคือไม่มีอะไรจำกัด — key ที่ไม่ผูกของเขา *กว้างขึ้น* · กติกานั้นไม่เปลี่ยน
แต่การเอาออกต้องเตือน เหมือนที่ `join` เตือนกรณีกลับด้าน
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import AuditLog
from app.db.session import session_scope

CODING = "http://dgx03:8000/v1/chat/completions"
REPLY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "x",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


@pytest.fixture
def upstream():
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def ask(client, key, model="coding"):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def models(client, key) -> set[str]:
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def user(client, external_id, role="member"):
    return admin(client, "POST", "/admin/users",
                 json={"external_id": external_id, "role": role}).json()


def workspace(client, code, aliases):
    ws = admin(client, "POST", "/admin/workspaces", json={"code": code, "name": code}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/models", json={"models": list(aliases)})
    return ws


def join(client, ws, person):
    return admin(client, "POST", f"/admin/workspaces/{ws['id']}/join",
                 json={"user_id": person["id"]})


def leave(client, ws, person):
    return admin(client, "DELETE", f"/admin/workspaces/{ws['id']}/members/{person['id']}")


def issue(client, person, **extra):
    r = admin(client, "POST", "/admin/api-keys",
              json={"user_id": person["id"], "name": "lab", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def enrolled(client, external_id="stu1", code="CS101", aliases=("coding",), **key):
    """คนหนึ่งคนที่อยู่ในวิชา พร้อม key ที่ออกให้ภายใต้วิชานั้น"""
    person = user(client, external_id)
    ws = workspace(client, code, aliases)
    join(client, ws, person)
    return person, ws, issue(client, person, workspace_id=ws["id"], **key)


# ── ตัว defect ───────────────────────────────────────────────────────────────

def test_the_key_stops_when_its_owner_is_taken_out_of_the_workspace(client, upstream):
    person, ws, key = enrolled(client)
    assert ask(client, key["api_key"]).status_code == 200

    assert leave(client, ws, person).json()["status"] == "removed"

    after = ask(client, key["api_key"])
    assert after.status_code == 403
    error = after.json()["error"]
    assert error["code"] == "MODEL_NOT_PERMITTED"
    assert error["details"]["reason_code"] == "workspace_left"
    # ข้อความต้องพาไปหาทางแก้ที่ถูก — ไม่ใช่ "ขอโมเดลเพิ่ม" แต่ "ขอกลับเข้ากลุ่ม"
    assert "no longer a member" in error["message"]


def test_the_catalogue_goes_with_it(client, upstream):
    """ลิสต์ที่โชว์ต้องตรงกับที่เรียกได้จริง — เดิม /v1/models ยังโชว์โมเดลของวิชาครบ"""
    person, ws, key = enrolled(client)
    assert models(client, key["api_key"]) == {"coding"}

    leave(client, ws, person)

    assert models(client, key["api_key"]) == set()
    catalog = client.get("/v1/catalog", headers=auth(key["api_key"])).json()
    assert catalog["sections"] == []
    assert catalog["access"]["restricted"] is True
    assert catalog["access"]["reason_code"] == "workspace_left"


@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {"model": "coding", "messages": [{"role": "user", "content": "x"}]}),
    ("/v1/messages", {"model": "coding", "max_tokens": 16,
                      "messages": [{"role": "user", "content": "x"}]}),
    ("/v1/responses", {"model": "coding", "input": "x"}),
])
def test_every_surface_refuses_it(client, path, body):
    """ด่านเดียวกันทุกทางเข้า — ไม่มี respx: ถ้าคำขอหลุดไปถึง backend เทสนี้ล้มเพราะต่อไม่ได้"""
    person, ws, key = enrolled(client)
    leave(client, ws, person)

    response = client.post(path, headers={**auth(key["api_key"]),
                                          "anthropic-version": "2023-06-01"}, json=body)
    assert response.status_code == 403, response.text
    assert "no longer a member" in response.text


def test_putting_the_person_back_makes_the_same_key_work_again(client, upstream):
    """เหตุผลที่ไม่เพิกถอน: เพิกถอนกู้คืนไม่ได้ · ใส่กลับต้องพอ ไม่ต้องออกใบใหม่"""
    person, ws, key = enrolled(client)
    leave(client, ws, person)
    assert ask(client, key["api_key"]).status_code == 403

    assert join(client, ws, person).json()["status"] == "joined"

    assert ask(client, key["api_key"]).status_code == 200
    assert models(client, key["api_key"]) == {"coding"}


def test_the_key_is_stopped_not_revoked(client, upstream):
    person, ws, key = enrolled(client)
    leave(client, ws, person)

    listed = admin(client, "GET", "/admin/api-keys").json()["data"]
    assert next(k for k in listed if k["id"] == key["id"])["revoked"] is False
    # และยังยืนยันตัวตนได้ — ที่หายคือสิทธิ์เรียกโมเดล ไม่ใช่ตัว key
    assert client.get("/v1/me", headers=auth(key["api_key"])).status_code == 200


def test_somebody_elses_key_for_the_same_workspace_is_untouched(client, upstream):
    person, ws, key = enrolled(client)
    classmate = user(client, "stu2")
    join(client, ws, classmate)
    theirs = issue(client, classmate, workspace_id=ws["id"])

    leave(client, ws, person)

    assert ask(client, theirs["api_key"]).status_code == 200
    assert ask(client, key["api_key"]).status_code == 403


def test_a_list_on_the_key_does_not_bring_anything_back(client, upstream):
    """รายการบน key ทำได้แค่แคบลง — เอามาเติมให้ของที่ไม่เหลืออะไรไม่ได้"""
    person, ws, key = enrolled(client, models=["coding"])
    leave(client, ws, person)
    assert ask(client, key["api_key"]).status_code == 403
    assert models(client, key["api_key"]) == set()


# ── คำตอบของการเอาออก ต้องบอกเองว่าอะไรเปลี่ยน ───────────────────────────────

def test_the_leave_response_names_the_keys_that_stopped(client):
    person, ws, key = enrolled(client)
    spare = issue(client, person)                      # ไม่ผูก workspace — ไม่ใช่ใบที่หยุด

    body = leave(client, ws, person).json()

    assert body["keys_stopped"] == [
        {"id": key["id"], "name": "lab", "key_prefix": key["key_prefix"]}
    ]
    assert spare["id"] not in [k["id"] for k in body["keys_stopped"]]
    assert "stopped working" in body["warning"]
    assert "not revoked" in body["warning"]
    # ไม่มีความลับในคำตอบ
    assert key["api_key"] not in str(body)


def test_keys_already_dead_are_not_reported_as_stopped(client):
    person, ws, key = enrolled(client)
    admin(client, "DELETE", f"/admin/api-keys/{key['id']}")           # เพิกถอนไปก่อนแล้ว
    assert leave(client, ws, person).json()["keys_stopped"] == []


def test_the_audit_row_says_which_keys_stopped(client):
    person, ws, key = enrolled(client)
    leave(client, ws, person)

    async def row():
        async with session_scope() as session:
            return (await session.execute(
                select(AuditLog).where(AuditLog.action == "workspace.leave")
            )).scalar_one()

    entry = client.portal.call(row)
    assert entry.target_id == ws["id"]
    assert entry.payload["user_id"] == person["id"]
    assert entry.payload["keys_stopped"] == [
        {"id": key["id"], "name": "lab", "key_prefix": key["key_prefix"]}
    ]
    assert key["api_key"] not in str(entry.payload)


# ── ออกจากกลุ่มสุดท้าย = กว้างขึ้น · กติกาเดิม แต่ต้องเตือน ────────────────────

def test_leaving_the_last_workspace_widens_unbound_keys_and_the_response_says_so(client):
    person = user(client, "stu3")
    ws = workspace(client, "CS101", ["coding"])
    join(client, ws, person)
    laptop = issue(client, person)                     # ไม่ผูก — สิทธิ์มาจากกลุ่มของเจ้าของ
    assert models(client, laptop["api_key"]) == {"coding"}

    body = leave(client, ws, person).json()

    # พฤติกรรมที่ออกแบบไว้ และไม่ได้เปลี่ยน: ไม่อยู่กลุ่มไหน = ไม่มีอะไรจำกัด
    now = models(client, laptop["api_key"])
    assert now > {"coding"}
    # สิ่งที่เพิ่ม: บอกคนที่เพิ่งกดว่าเขาเพิ่งแจกอะไรออกไป
    assert body["access_widened"] is True
    assert set(body["models_gained"]) == now - {"coding"}
    assert [k["id"] for k in body["keys_widened"]] == [laptop["id"]]
    assert "in no workspace" in body["warning"]
    for alias in body["models_gained"]:
        assert alias in body["warning"]


def test_leaving_one_of_two_workspaces_widens_nothing_and_warns_about_nothing(client):
    person = user(client, "stu4")
    cs = workspace(client, "CS101", ["coding"])
    art = workspace(client, "ART200", ["gemma-vision"])
    join(client, cs, person)
    join(client, art, person)
    laptop = issue(client, person)

    body = leave(client, art, person).json()

    assert models(client, laptop["api_key"]) == {"coding"}
    assert body["access_widened"] is False
    assert body["models_gained"] == []
    assert body["keys_widened"] == []
    assert body["keys_stopped"] == []
    assert body["warning"] == ""


def test_no_warning_when_the_workspace_already_allowed_everything(client):
    """เตือนเฉพาะตอนที่มีอะไรเพิ่มจริง — คำเตือนที่ขึ้นทุกครั้งคือคำเตือนที่ไม่มีใครอ่าน"""
    person = user(client, "stu5")
    ws = workspace(client, "ALL", ["coding", "gemma-vision", "muse-local"])
    join(client, ws, person)
    laptop = issue(client, person)
    everything = models(client, laptop["api_key"])

    body = leave(client, ws, person).json()

    assert models(client, laptop["api_key"]) == everything
    assert body["access_widened"] is False
    assert body["warning"] == ""


def test_the_audit_row_records_a_widening(client):
    person = user(client, "stu6")
    ws = workspace(client, "CS101", ["coding"])
    join(client, ws, person)
    issue(client, person)
    body = leave(client, ws, person).json()

    async def row():
        async with session_scope() as session:
            return (await session.execute(
                select(AuditLog).where(AuditLog.action == "workspace.leave")
            )).scalar_one()

    entry = client.portal.call(row)
    assert entry.payload["access_widened"] is True
    assert entry.payload["models_gained"] == body["models_gained"]


# ── ใครอยู่นอกกติกานี้ ─────────────────────────────────────────────────────────

def test_an_administrators_bound_key_does_not_depend_on_membership(client, upstream):
    """admin ไม่อยู่ใต้กติกา membership ข้อไหนเลย — ใบที่ผูกยังแคบตามวิชาเหมือนเดิม"""
    root = user(client, "root", role="admin")
    ws = workspace(client, "CS101", ["coding"])
    key = issue(client, root, workspace_id=ws["id"])       # ไม่เคยเป็นสมาชิก

    assert models(client, key["api_key"]) == {"coding"}
    assert ask(client, key["api_key"]).status_code == 200

    join(client, ws, root)
    body = leave(client, ws, root).json()
    assert body["keys_stopped"] == []
    assert "keep working" in body["warning"]
    assert ask(client, key["api_key"]).status_code == 200


def test_a_site_that_switched_membership_off_keeps_the_old_behaviour(writable_config):
    """FR-43 · ทางถอยต้องยังเป็นทางถอย

    `membership_grants_models: false` คือสวิตช์ที่มีไว้ให้อัปเกรดได้โดยไม่เปลี่ยนสิทธิ์ของ
    key ที่แจกไปแล้ว · ถ้ากติกาใหม่ไม่ฟังสวิตช์นี้ เครื่องที่ปิดไว้จะมี key ตายตอนอัปเดต
    โดยไม่มีทางเลือก — และคำตอบของการเอาออกต้องไม่บอกว่าใบหยุด ทั้งที่มันไม่ได้หยุด
    """
    import yaml
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import _bootstrap_key

    path = writable_config / "gateway.yaml"
    gateway = yaml.safe_load(path.read_text())
    gateway["membership_grants_models"] = False
    path.write_text(yaml.safe_dump(gateway, sort_keys=False, allow_unicode=True))

    with TestClient(create_app()) as client:
        client.admin_key = _bootstrap_key(client)
        person = user(client, "stu7")
        ws = workspace(client, "CS101", ["coding"])
        outside = issue(client, person, workspace_id=ws["id"])   # ไม่เป็นสมาชิกก็ออกได้
        assert models(client, outside["api_key"]) == {"coding"}

        join(client, ws, person)
        body = leave(client, ws, person).json()

        assert models(client, outside["api_key"]) == {"coding"}
        assert body["keys_stopped"] == []
        assert "keep working" in body["warning"]
        assert "membership_grants_models" in body["warning"]
        assert body["access_widened"] is False


# ── ออก key ให้คนนอกวิชา = ออกใบที่เรียกอะไรไม่ได้ · ปฏิเสธตั้งแต่ตอนออก ──────────

def test_a_key_cannot_be_issued_for_a_workspace_its_owner_is_not_in(client):
    person = user(client, "stu8")
    ws = workspace(client, "CS101", ["coding"])

    refused = admin(client, "POST", "/admin/api-keys",
                    json={"user_id": person["id"], "workspace_id": ws["id"], "name": "lab"})

    assert refused.status_code == 400, refused.text
    message = refused.json()["error"]["message"]
    assert "stu8" in message and "CS101" in message and "not a member" in message
    listed = admin(client, "GET", "/admin/api-keys", params={"user_id": person["id"]}).json()
    assert listed["data"] == []


def test_a_key_cannot_be_issued_for_a_workspace_that_does_not_exist(client):
    """admin ผ่านด่านความเป็นเจ้าของทุก id · บน PostgreSQL id ปลอมคือ FK violation = 500"""
    person = user(client, "stu9")
    refused = admin(client, "POST", "/admin/api-keys",
                    json={"user_id": person["id"], "workspace_id": "0" * 32})
    assert refused.status_code == 400
    assert "Workspace not found" in refused.json()["error"]["message"]
