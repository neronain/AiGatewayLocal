"""หลังลบอะไรก็ตามในหน้าผู้ดูแล ต้องไม่เหลือแถวที่ชี้ไปยังแถวที่ไม่มีอยู่

ทำไมต้องมีไฟล์นี้: SQLite ไม่บังคับ foreign key เว้นแต่จะตั้ง ``PRAGMA
foreign_keys=ON`` ซึ่งเกตเวย์ไม่ได้ตั้ง และจงใจไม่ตั้ง — พฤติกรรมฝั่ง SQLite ถูก
ตรึงไว้แล้ว · ผลคือทางลบทุกทางที่ "ผ่าน" บน SQLite ไม่ได้แปลว่าถูก มันแปลว่า
*ไม่มีใครตรวจ* และบน PostgreSQL ซึ่งบังคับจริง ทางเดียวกันนั้นตอบ HTTP 500 กลับมา

เคยหลุดมาแล้วสามที่ ทั้งสามผ่าน SQLite และล้มบน PostgreSQL:

  * ลบ workspace ที่เคยมีคีย์ผูกอยู่แล้วถูกเพิกถอน — api_keys_course_id_fkey
  * purge คีย์ที่มีเพดานของตัวเอง — quota_policies_api_key_id_fkey
  * ลบมัดที่มีโควตาเล็งอยู่ — quota_policies_access_group_id_fkey

เทสในไฟล์นี้ไม่พึ่ง FK ของฐานข้อมูล: มันเดินทุก foreign key ใน metadata แล้วถาม
ตรง ๆ ว่ามีค่าไหนชี้ไปยังแถวที่หายไปหรือเปล่า · จึงจับของแบบนี้ได้ **บน SQLite ด้วย**
ซึ่งเป็นค่าเริ่มต้นที่คนรันจริงทุกวัน ไม่ใช่เฉพาะตอนมีเซิร์ฟเวอร์ Postgres ให้รัน
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.models import Base
from app.db.session import session_scope


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


async def _dangling() -> list[str]:
    """ทุกค่าที่ชี้ไปยังแถวที่ไม่มีอยู่ · ว่าง = ฐานข้อมูลสอดคล้องกันเอง"""
    found: list[str] = []
    async with session_scope() as session:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                for fk in column.foreign_keys:
                    parent = fk.column
                    rows = await session.execute(
                        select(column).where(
                            column.is_not(None), column.notin_(select(parent))
                        ).limit(5)
                    )
                    found += [
                        f"{table.name}.{column.name} -> "
                        f"{parent.table.name}.{parent.name} = {value}"
                        for (value,) in rows
                    ]
    return found


def dangling() -> list[str]:
    return asyncio.run(_dangling())


def _build_everything(client) -> dict[str, str]:
    """ของครบทุกชนิดที่มี foreign key ชี้หากัน แล้วรื้อทิ้งทีละอย่าง"""
    admin = auth(client.admin_key)
    bundle = client.post("/admin/access-groups", headers=admin,
                         json={"name": "ri-bundle", "models": ["coding"]}).json()
    ws = client.post("/admin/workspaces", headers=admin,
                     json={"code": "RI101", "name": "RI101"}).json()
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=admin,
                json={"models": ["coding"], "access_groups": [bundle["id"]]})

    student = client.post("/admin/users", headers=admin,
                          json={"external_id": "ri-student"}).json()
    client.post(f"/admin/workspaces/{ws['id']}/join", headers=admin,
                json={"user_id": student["id"]})

    pinned = client.post("/admin/api-keys", headers=admin,
                         json={"user_id": student["id"], "workspace_id": ws["id"],
                               "name": "pinned"}).json()
    ci = client.post("/admin/api-keys", headers=admin,
                     json={"user_id": student["id"], "name": "ci"}).json()

    client.post("/admin/quota-policies", headers=admin,
                json={"scope": "workspace", "workspace_id": ws["id"], "max_requests": 9})
    client.post("/admin/quota-policies", headers=admin,
                json={"scope": "user", "user_id": student["id"], "max_requests": 8})
    client.post("/admin/quota-policies", headers=admin,
                json={"scope": "key", "api_key_id": ci["id"], "max_requests": 7})
    capped = client.post("/admin/quota-policies", headers=admin,
                         json={"scope": "global", "access_group_id": bundle["id"],
                               "max_requests": 6}).json()

    return {"ws": ws["id"], "bundle": bundle["id"], "student": student["id"],
            "pinned": pinned["id"], "ci": ci["id"], "capped": capped["id"]}


def test_nothing_dangles_after_the_admin_delete_paths_run(client):
    admin = auth(client.admin_key)
    ids = _build_everything(client)
    assert dangling() == [], "ตั้งต้นต้องสะอาดก่อน"

    # เพิกถอนคีย์ที่ผูกไว้ แล้วเอาคนออก — สองอย่างที่กันการลบ workspace ไว้
    client.delete(f"/admin/api-keys/{ids['pinned']}", headers=admin)
    client.delete(f"/admin/workspaces/{ids['ws']}/members/{ids['student']}", headers=admin)
    assert client.delete(f"/admin/workspaces/{ids['ws']}", headers=admin).status_code == 200

    # purge คีย์ที่มีเพดานของตัวเอง
    client.delete(f"/admin/api-keys/{ids['ci']}", headers=admin)
    assert client.delete(f"/admin/api-keys/{ids['ci']}/purge",
                         headers=admin).status_code == 200

    # ลบมัด — ต้องเอาโควตาที่เล็งมันอยู่ออกก่อน ซึ่งคำปฏิเสธบอกไว้
    assert client.delete(f"/admin/access-groups/{ids['bundle']}",
                         headers=admin).status_code == 400
    client.delete(f"/admin/quota-policies/{ids['capped']}", headers=admin)
    assert client.delete(f"/admin/access-groups/{ids['bundle']}",
                         headers=admin).status_code == 200

    # กวาดใบที่เหลือทั้งหมดปิดท้าย
    client.post("/admin/api-keys/purge-revoked", headers=admin)

    assert dangling() == []


@pytest.mark.sqlite_only
def test_the_check_itself_catches_a_planted_orphan(client):
    """เทสที่ผ่านเพราะไม่ได้ตรวจอะไร ไม่ใช่เทส — ปลูกของเสียแล้วต้องเจอ

    ปลูกได้เฉพาะบน SQLite ซึ่งไม่บังคับ FK · บน PostgreSQL ตัว INSERT/UPDATE เองจะ
    ถูกปฏิเสธตั้งแต่แรก ซึ่งก็คือสิ่งที่เทสนี้อยากพิสูจน์อยู่แล้ว จึงข้ามไปได้
    """
    admin = auth(client.admin_key)
    student = client.post("/admin/users", headers=admin,
                          json={"external_id": "ri-orphan"}).json()
    key = client.post("/admin/api-keys", headers=admin,
                      json={"user_id": student["id"], "name": "k"}).json()

    async def plant() -> None:
        from app.db.models import ApiKey
        async with session_scope() as session:
            row = await session.get(ApiKey, key["id"])
            row.workspace_id = "no-such-workspace"
            await session.commit()

    asyncio.run(plant())
    assert any("api_keys.course_id" in d for d in dangling())
