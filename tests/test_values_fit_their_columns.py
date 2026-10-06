"""ข้อความที่เขียนลงฐานข้อมูลต้องไม่ยาวเกินคอลัมน์ของมัน — แม้ SQLite จะไม่ว่าอะไร

เคสจริง (ตรวจ 2026-10-06 กับ production ที่รันบน PostgreSQL): `audit_logs.target_id`
เป็น VARCHAR(128) แต่การลบนโยบายโควตาเขียน
``"<id 32 ตัว> scope=workspace window=month requests=1000 user=- workspace=<id 32 ตัว>
model=coding"`` ลงไป — 138 ตัวอักษรสำหรับนโยบายธรรมดา ๆ ที่ผูก workspace กับโมเดล ·
PostgreSQL ปฏิเสธแถว audit ตอน commit คำขอจบด้วย HTTP 500 และ **การลบถูก rollback**
ผู้ดูแลกดลบแล้วนโยบายยังอยู่ โดยไม่มีอะไรบนจอบอกว่าทำไม

SQLite ไม่บังคับความยาวของ VARCHAR เลย ชุดเทสจึงผ่านมาตลอด · เทสในไฟล์นี้จึงไม่รอให้
ฐานข้อมูลโวย แต่อ่านแถวที่เขียนลงไปจริงแล้ววัดความยาวเทียบกับคอลัมน์เอง — ล้มบน SQLite
ได้ และยังถูกต้องเมื่อรันบน PostgreSQL
"""

from __future__ import annotations

import pytest
from sqlalchemy import String, select

from app.db.models import AuditLog
from app.db.session import session_scope


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def audit_rows(client, action: str | None = None) -> list[AuditLog]:
    async def read():
        async with session_scope() as session:
            stmt = select(AuditLog).order_by(AuditLog.ts)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            return list((await session.execute(stmt)).scalars())

    return client.portal.call(read)


def overflowing(rows) -> list[str]:
    """ทุกค่าที่ยาวเกินคอลัมน์ของมัน ในรูปที่อ่านแล้วรู้ว่าแถวไหน คอลัมน์ไหน"""
    bad = []
    for row in rows:
        for column in row.__table__.columns:
            if not isinstance(column.type, String) or column.type.length is None:
                continue
            value = getattr(row, column.key)
            if value is not None and len(value) > column.type.length:
                bad.append(f"{row.action}: {column.name} is {len(value)} chars, "
                           f"VARCHAR({column.type.length})")
    return bad


def test_deleting_a_workspace_and_model_policy_writes_an_audit_row_that_fits(client):
    """ตัวที่พังบน production: นโยบายที่ผูกทั้ง workspace และโมเดล"""
    ws = admin(client, "POST", "/admin/workspaces", json={"code": "CS101", "name": "CS101"}).json()
    policy = admin(client, "POST", "/admin/quota-policies", json={
        "scope": "workspace", "workspace_id": ws["id"], "model_alias": "coding",
        "window": "month", "max_requests": 1000, "name": "exam fortnight",
    }).json()

    gone = admin(client, "DELETE", f"/admin/quota-policies/{policy['id']}")
    assert gone.status_code == 200, gone.text

    rows = audit_rows(client, "quota.delete")
    assert len(rows) == 1
    assert overflowing(rows) == []
    # id อยู่ในช่อง id ตรง ๆ — หาแถวนี้เจอด้วยการเทียบค่า ไม่ต้อง LIKE
    assert rows[0].target_id == policy["id"]
    # และสิ่งที่นโยบายเคยเป็นยังอยู่ครบ เพราะแถวของมันไม่อยู่ให้ดูแล้ว
    assert rows[0].payload["scope"] == "workspace"
    assert rows[0].payload["workspace_id"] == ws["id"]
    assert rows[0].payload["model_alias"] == "coding"
    assert rows[0].payload["window"] == "month"
    assert rows[0].payload["max_requests"] == 1000


def test_purging_a_key_with_a_long_name_writes_an_audit_row_that_fits(client):
    """ชื่อ key ยาวได้ถึง 128 ตัว · id 32 + ชื่อ + prefix เคยถูกต่อกันลง target_id"""
    person = admin(client, "POST", "/admin/users", json={"external_id": "ci"}).json()
    name = "ci token for the nightly evaluation pipeline on the research cluster (owner: platform)"
    key = admin(client, "POST", "/admin/api-keys",
                json={"user_id": person["id"], "name": name}).json()
    admin(client, "DELETE", f"/admin/api-keys/{key['id']}")

    gone = admin(client, "DELETE", f"/admin/api-keys/{key['id']}/purge")
    assert gone.status_code == 200, gone.text

    rows = audit_rows(client, "apikey.purge")
    assert overflowing(rows) == []
    assert rows[0].target_id == key["id"]
    assert rows[0].payload["name"] == name
    assert rows[0].payload["key_prefix"] == key["key_prefix"]


def test_the_sweep_records_how_many_it_took_without_using_the_id_column(client):
    person = admin(client, "POST", "/admin/users", json={"external_id": "ci"}).json()
    for _ in range(2):
        key = admin(client, "POST", "/admin/api-keys", json={"user_id": person["id"]}).json()
        admin(client, "DELETE", f"/admin/api-keys/{key['id']}")

    assert admin(client, "POST", "/admin/api-keys/purge-revoked").json()["purged"] == 2

    row = audit_rows(client, "apikey.purge_revoked")[0]
    assert row.target_id == ""
    assert row.payload["purged"] == 2
    assert row.payload["older_than_days"] == 0


def test_nothing_the_admin_plane_writes_to_the_audit_log_overflows(client, writable_config):
    """เดินทุกเส้นทางที่เขียน audit ด้วยค่าที่ยาวที่สุดที่ฟอร์มยอมรับ แล้ววัดทุกแถว"""
    long_id = "u" * 128
    person = admin(client, "POST", "/admin/users",
                   json={"external_id": long_id, "display_name": "d" * 255,
                         "email": "e" * 255}).json()
    assert person["external_id"] == long_id
    ws = admin(client, "POST", "/admin/workspaces",
               json={"code": "c" * 64, "name": "n" * 255, "term": "t" * 32}).json()
    group = admin(client, "POST", "/admin/access-groups",
                  json={"name": "g" * 64, "description": "x" * 255, "models": ["coding"]}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/models",
          json={"models": ["coding"], "access_groups": [group["id"]]})
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/join", json={"user_id": person["id"]})
    key = admin(client, "POST", "/admin/api-keys",
                json={"user_id": person["id"], "workspace_id": ws["id"], "name": "k" * 128}).json()
    admin(client, "PATCH", f"/admin/api-keys/{key['id']}", json={"models": ["coding"]})
    admin(client, "PATCH", f"/admin/users/{person['id']}", json={"display_name": "z" * 255})
    admin(client, "DELETE", f"/admin/workspaces/{ws['id']}/members/{person['id']}")
    admin(client, "DELETE", f"/admin/api-keys/{key['id']}")
    admin(client, "DELETE", f"/admin/api-keys/{key['id']}/purge")
    admin(client, "PATCH", f"/admin/access-groups/{group['id']}", json={"description": "y" * 255})

    rows = audit_rows(client)
    assert len(rows) >= 10, [r.action for r in rows]
    assert overflowing(rows) == []


def test_audit_itself_keeps_an_oversized_target_from_reaching_the_column(client):
    """ด่านสุดท้าย: ต่อให้ผู้เรียกในอนาคตยัดข้อความยาวลง target_id อีก แถวก็ยังเขียนได้

    id นำหน้ายังอยู่ในคอลัมน์ ข้อความเต็มย้ายไปอยู่ใน payload — ไม่มีอะไรหาย
    """
    from app.core.audit import fit_target

    policy_id = "a" * 32
    text = f"{policy_id} scope=workspace window=month requests=1000 user=- " \
           f"workspace={'b' * 32} model=coding"
    assert len(text) > 128

    target, payload = fit_target(text, {"kept": True})
    assert target == policy_id
    assert payload == {"kept": True, "target_detail": text}

    # ไม่มีช่องว่างให้ตัด: ตัดให้พอดีคอลัมน์ และยังเก็บตัวเต็มไว้
    blob = "x" * 500
    target, payload = fit_target(blob, None)
    assert target == "x" * 128
    assert payload["target_detail"] == blob

    # ของที่พอดีอยู่แล้วต้องไม่ถูกแตะ — รวมถึงค่าที่มีช่องว่างอย่างชื่อคน
    assert fit_target("Somchai Jaidee", None) == ("Somchai Jaidee", {})
    assert fit_target("y" * 128, {"a": 1}) == ("y" * 128, {"a": 1})


def test_audit_does_not_edit_the_dictionary_it_was_handed(client):
    """หลายเส้นทางส่ง body ของคำขอเข้ามาตรง ๆ แล้วคืน body เดิมกลับไปให้ผู้เรียก"""
    from app.core.audit import fit_target

    body = {"role": "member"}
    _, payload = fit_target("z" * 300, body)
    assert body == {"role": "member"}
    assert payload is not body


# ── ฟอร์มรับข้อความยาวเกินคอลัมน์ไม่ได้ — ตอบ 400 พร้อมบอกช่อง ไม่ใช่ 500 จากฐานข้อมูล ──
#
# (เกตเวย์ตัวนี้แปลง validation error ของ FastAPI เป็น 400 INVALID_REQUEST ทั้งหมด —
# ดู validation_handler ใน app/main.py — จึงไม่มี 422 ให้เห็น)

@pytest.mark.parametrize("body, field", [
    ({"external_id": "u1", "display_name": "d" * 256}, "display_name"),
    ({"external_id": "u1", "email": "e" * 256}, "email"),
    ({"external_id": "u" * 129}, "external_id"),
])
def test_a_user_field_longer_than_its_column_is_refused(client, body, field):
    response = admin(client, "POST", "/admin/users", json=body)
    assert response.status_code == 400, response.text
    assert field in str(response.json()["error"]["details"])
    assert audit_rows(client, "user.create") == []


def test_a_key_name_longer_than_its_column_is_refused(client):
    person = admin(client, "POST", "/admin/users", json={"external_id": "u1"}).json()
    response = admin(client, "POST", "/admin/api-keys",
                     json={"user_id": person["id"], "name": "k" * 129})
    assert response.status_code == 400, response.text
    assert "name" in str(response.json()["error"]["details"])
    assert admin(client, "GET", "/admin/api-keys").json()["data"] == [] or all(
        k["user_id"] != person["id"] for k in admin(client, "GET", "/admin/api-keys").json()["data"]
    )


@pytest.mark.parametrize("change", [
    {"display_name": "d" * 256},
    {"email": "e" * 256},
    {"display_name": ["not", "text"]},
    {"display_name": None},
])
def test_editing_a_user_checks_the_same_lengths(client, change):
    """PATCH รับ JSON object เปล่า ๆ ไม่ผ่าน pydantic — เดิมค่าอะไรก็ไหลลง setattr ตรง ๆ"""
    person = admin(client, "POST", "/admin/users",
                   json={"external_id": "u1", "display_name": "before"}).json()
    response = admin(client, "PATCH", f"/admin/users/{person['id']}", json=change)
    assert response.status_code == 400, response.text
    listed = admin(client, "GET", "/admin/users").json()["data"]
    assert next(u for u in listed if u["id"] == person["id"])["display_name"] == "before"


def test_clearing_an_email_is_still_allowed(client):
    person = admin(client, "POST", "/admin/users",
                   json={"external_id": "u1", "email": "a@example.com"}).json()
    response = admin(client, "PATCH", f"/admin/users/{person['id']}", json={"email": None})
    assert response.status_code == 200, response.text
    assert response.json()["email"] is None


@pytest.mark.parametrize("body", [
    {"code": "CS101", "name": "n" * 256},
    {"code": "CS101", "name": "ok", "term": "t" * 33},
    {"code": "c" * 65, "name": "ok"},
])
def test_a_workspace_field_longer_than_its_column_is_refused(client, body):
    assert admin(client, "POST", "/admin/workspaces", json=body).status_code == 400


def test_an_access_group_description_longer_than_its_column_is_refused(client, writable_config):
    created = admin(client, "POST", "/admin/access-groups",
                    json={"name": "set", "description": "x" * 256, "models": ["coding"]})
    assert created.status_code == 400

    group = admin(client, "POST", "/admin/access-groups",
                  json={"name": "set", "models": ["coding"]}).json()
    for change in ({"description": "x" * 256}, {"name": "g" * 65}, {"name": "  "},
                   {"enabled": "yes"}):
        edited = admin(client, "PATCH", f"/admin/access-groups/{group['id']}", json=change)
        assert edited.status_code == 400, (change, edited.text)
    still = admin(client, "GET", "/admin/access-groups").json()["data"][0]
    assert (still["name"], still["description"], still["enabled"]) == ("set", "", True)


def test_a_membership_role_longer_than_its_column_is_refused(client):
    person = admin(client, "POST", "/admin/users", json={"external_id": "u1"}).json()
    ws = admin(client, "POST", "/admin/workspaces", json={"code": "CS101", "name": "x"}).json()
    response = admin(client, "POST", f"/admin/workspaces/{ws['id']}/join",
                     json={"user_id": person["id"], "role": "r" * 33})
    assert response.status_code == 400, response.text
    assert admin(client, "GET", "/admin/workspaces").json()["data"][0]["members"] == []


def test_first_run_setup_refuses_a_display_name_longer_than_its_column(client):
    """หน้าแรกที่ทุกคนเห็น — 500 ตรงนี้คือการติดตั้งที่ดูเหมือนพังตั้งแต่ยังไม่เริ่ม"""
    from sqlalchemy import delete

    from app.db.models import ApiKey, User

    async def back_to_first_run():
        # conftest กับ startup สร้าง admin ไว้ให้แล้ว ซึ่งปิด /auth/setup · เอาออกเพื่อ
        # ให้ได้สภาพเดียวกับเครื่องที่เพิ่งติดตั้งและยังไม่มีใครตั้งผู้ดูแล
        async with session_scope() as session:
            await session.execute(delete(ApiKey))
            await session.execute(delete(User))

    client.portal.call(back_to_first_run)
    assert client.get("/auth/status").json()["needs_setup"] is True

    refused = client.post("/auth/setup", json={
        "username": "root", "password": "correct horse battery", "display_name": "d" * 256,
    })
    assert refused.status_code == 400, refused.text
    assert client.get("/auth/status").json()["needs_setup"] is True

    accepted = client.post("/auth/setup", json={
        "username": "root", "password": "correct horse battery", "display_name": "d" * 255,
    })
    assert accepted.status_code == 201, accepted.text
