"""จัดการ key ของบัญชีต้องเป็น "คนที่ลงชื่อเข้าใช้" ไม่ใช่ "key ใบหนึ่ง" — และต้องทิ้งร่องรอย

เคสจริง (ตรวจ 2026-10-06 บน a5e9295): นักพัฒนาคนหนึ่งมี key สองใบ ใบหนึ่งรั่ว · ใบที่รั่ว
ออกใบใหม่ให้ตัวเองไม่ได้ (403 — กันไว้ตั้งแต่ต้น) แต่ `GET /v1/me/api-keys` ได้ 200 พร้อม
รายการ key ทั้งหมดของเจ้าของ และ `DELETE /v1/me/api-keys/<ใบ production>` ได้ 200 —
ใบ production ตายทันที · ทั้งหมดนี้ไม่มีแถวใน audit_logs สักแถว

กติกาเดียวสำหรับทั้งสามอย่าง (ดู `_require_console` ใน app/api/auth.py): ดูรายการ ·
ออกใบใหม่ · เพิกถอน คือความสามารถเดียวกัน — "ตัดสินว่าบัญชีนี้มี credential ใบไหนบ้าง" —
และต้องใช้ session ของคอนโซลทั้งหมด · key ยังอ่านข้อมูลของ *ตัวมันเอง* ได้ที่ /v1/me/key

ครึ่งหลังของไฟล์: การเปลี่ยนสถานะสี่อย่างที่เคยไม่เขียน audit เลย — ออก key เอง ·
เพิกถอน key เอง · เปลี่ยนรหัสผ่าน · สั่งอัปเดตเกตเวย์
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.passwords import SESSION_COOKIE_INSECURE, hash_password
from app.db.models import ApiKey, AuditLog, User
from app.db.session import session_scope

PASSWORD = "correct-horse-battery"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def person(client, external_id="dev1", role="member") -> dict:
    """บัญชีที่มีรหัสผ่าน — เข้าคอนโซลได้ และผู้ดูแลออก key ให้ได้"""
    made = admin(client, "POST", "/admin/users",
                 json={"external_id": external_id, "role": role}).json()

    async def set_password():
        async with session_scope() as session:
            (await session.get(User, made["id"])).password_hash = hash_password(PASSWORD)

    client.portal.call(set_password)
    return made


def key_for(client, who, name="k") -> dict:
    return admin(client, "POST", "/admin/api-keys",
                 json={"user_id": who["id"], "name": name}).json()


def sign_in(client, username="dev1"):
    client.cookies.clear()
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


def audit(client, action: str) -> list[AuditLog]:
    async def read():
        async with session_scope() as session:
            return list((await session.execute(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.ts)
            )).scalars())

    return client.portal.call(read)


def works(client, plaintext: str) -> bool:
    client.cookies.clear()
    return client.get("/v1/me", headers=auth(plaintext)).status_code == 200


# ── key ที่รั่วทำอะไรกับใบอื่นของเจ้าของไม่ได้ ─────────────────────────────────

def test_a_leaked_key_cannot_revoke_its_owners_other_keys(client):
    dev = person(client)
    leaked, production = key_for(client, dev, "old laptop"), key_for(client, dev, "production")

    kill = client.delete(f"/v1/me/api-keys/{production['id']}", headers=auth(leaked["api_key"]))

    assert kill.status_code == 403, kill.text
    assert works(client, production["api_key"])
    row = next(k for k in admin(client, "GET", "/admin/api-keys").json()["data"]
               if k["id"] == production["id"])
    assert row["revoked"] is False
    assert audit(client, "apikey.revoke") == []


def test_a_key_cannot_even_revoke_itself(client):
    """กติกาเดียว ไม่มีข้อยกเว้นให้ต้องจำ — เพิกถอนเป็นงานของคนที่ลงชื่อเข้าใช้"""
    dev = person(client)
    key = key_for(client, dev)
    assert client.delete(f"/v1/me/api-keys/{key['id']}",
                         headers=auth(key["api_key"])).status_code == 403
    assert works(client, key["api_key"])


def test_a_leaked_key_cannot_list_its_owners_keys(client):
    dev = person(client)
    leaked, production = key_for(client, dev, "old laptop"), key_for(client, dev, "production")

    listing = client.get("/v1/me/api-keys", headers=auth(leaked["api_key"]))

    assert listing.status_code == 403, listing.text
    assert production["id"] not in listing.text and "production" not in listing.text
    # คำตอบบอกทางที่ถูก: ใบนี้อ่านข้อมูลของตัวเองได้ที่ไหน
    assert "/v1/me/key" in listing.json()["error"]["message"]


def test_a_key_still_reads_its_own_details(client):
    dev = person(client)
    key = key_for(client, dev, "laptop")
    mine = client.get("/v1/me/key", headers=auth(key["api_key"]))
    assert mine.status_code == 200
    assert mine.json()["key"]["prefix"] == key["key_prefix"]
    assert mine.json()["key"]["label"] == "laptop"


def test_the_owner_signed_in_lists_and_revokes_as_before(client):
    dev = person(client)
    leaked, production = key_for(client, dev, "old laptop"), key_for(client, dev, "production")
    sign_in(client)

    listed = client.get("/v1/me/api-keys")
    assert listed.status_code == 200
    assert {k["name"] for k in listed.json()["data"]} == {"old laptop", "production"}

    assert client.delete(f"/v1/me/api-keys/{leaked['id']}").status_code == 200
    assert not works(client, leaked["api_key"])
    assert works(client, production["api_key"])


def test_a_manager_holding_a_key_is_held_to_the_same_rule(client):
    """role ไม่ได้เปลี่ยนอะไร: นี่คือเส้นทางของ "บัญชีฉัน" ไม่ใช่ของงานดูแล"""
    boss = person(client, "lecturer", role="manager")
    one, two = key_for(client, boss, "a"), key_for(client, boss, "b")
    assert client.get("/v1/me/api-keys", headers=auth(one["api_key"])).status_code == 403
    assert client.delete(f"/v1/me/api-keys/{two['id']}",
                         headers=auth(one["api_key"])).status_code == 403
    assert works(client, two["api_key"])


# ── ร่องรอย ──────────────────────────────────────────────────────────────────

def test_issuing_yourself_a_key_is_written_to_the_audit_log(client):
    dev = person(client)
    sign_in(client)

    created = client.post("/v1/me/api-keys", json={"name": "laptop"}).json()

    rows = [r for r in audit(client, "apikey.create") if r.target_type == "apikey"]
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == dev["id"]
    assert row.target_id == created["id"]
    assert row.payload["owner"] == dev["id"]
    assert row.payload["key_prefix"] == created["key_prefix"]
    assert row.payload["name"] == "laptop"
    assert row.payload["via"] == "self-service"
    _assert_no_secret(client, row, created["api_key"])


def test_revoking_your_own_key_is_written_to_the_audit_log(client):
    dev = person(client)
    key = key_for(client, dev, "old laptop")
    sign_in(client)

    assert client.delete(f"/v1/me/api-keys/{key['id']}").status_code == 200

    rows = audit(client, "apikey.revoke")
    assert len(rows) == 1
    assert rows[0].actor_user_id == dev["id"]
    assert (rows[0].target_type, rows[0].target_id) == ("apikey", key["id"])
    assert rows[0].payload["key_prefix"] == key["key_prefix"]
    assert rows[0].payload["name"] == "old laptop"
    _assert_no_secret(client, rows[0], key["api_key"])


def test_revoking_twice_keeps_the_first_moment_and_writes_one_row(client):
    dev = person(client)
    key = key_for(client, dev)
    sign_in(client)
    client.delete(f"/v1/me/api-keys/{key['id']}")

    async def revoked_at():
        async with session_scope() as session:
            return (await session.get(ApiKey, key["id"])).revoked_at

    first = client.portal.call(revoked_at)
    assert client.delete(f"/v1/me/api-keys/{key['id']}").status_code == 200
    assert client.portal.call(revoked_at) == first
    assert len(audit(client, "apikey.revoke")) == 1


def test_a_key_an_administrator_issues_is_recorded_with_which_key(client):
    """เดิมแถวบอกแค่ว่า "ออก key ให้คนนี้" — คนที่มีห้าใบ ไม่รู้ว่าแถวไหนคือใบไหน"""
    dev = person(client)
    key = key_for(client, dev, "ci")

    row = next(r for r in audit(client, "apikey.create") if r.target_id == dev["id"])
    assert row.payload["key_id"] == key["id"]
    assert row.payload["key_prefix"] == key["key_prefix"]
    assert row.payload["name"] == "ci"
    _assert_no_secret(client, row, key["api_key"])


def test_changing_a_password_is_written_to_the_audit_log_without_the_password(client):
    dev = person(client)
    sign_in(client)
    new = "a-brand-new-secret-phrase"

    changed = client.post("/auth/password",
                          json={"current_password": PASSWORD, "new_password": new})
    assert changed.status_code == 200, changed.text

    rows = audit(client, "user.password")
    assert len(rows) == 1
    assert rows[0].actor_user_id == dev["id"]
    assert (rows[0].target_type, rows[0].target_id) == ("user", dev["id"])
    assert rows[0].payload == {"other_sessions_signed_out": True}
    everything = f"{rows[0].payload} {rows[0].target_id} {rows[0].action}"
    assert new not in everything and PASSWORD not in everything


def test_a_refused_password_change_writes_nothing(client):
    person(client)
    sign_in(client)
    refused = client.post("/auth/password",
                          json={"current_password": "not-it", "new_password": "x" * 20})
    assert refused.status_code == 401
    assert audit(client, "user.password") == []


def test_asking_the_gateway_to_update_itself_is_written_to_the_audit_log(
    client, monkeypatch, tmp_path
):
    """การกระทำเดียวในคอนโซลที่แทนที่โค้ดทั้งตัวแล้ว restart — เดิมไม่มีแถว audit เลย"""
    from app.core import release

    # เครื่องเทสไม่มี path unit ของ systemd และต้องไม่เขียนไฟล์คำขอลงโฟลเดอร์ของ repo
    monkeypatch.setattr(release, "updates_are_wired", lambda: True)
    monkeypatch.setattr(release, "state_dir", lambda: tmp_path)

    asked = admin(client, "POST", "/admin/version/update", json={"skip_deps": True})

    assert asked.status_code == 200, asked.text
    assert asked.json()["accepted"] is True
    assert (tmp_path / release.REQUEST_NAME).exists(), "คำขอต้องถูกวางจริง ไม่ใช่แค่ถูกบันทึก"

    rows = audit(client, "version.update")
    assert len(rows) == 1
    assert rows[0].actor_user_id == _admin_id(client)
    assert rows[0].target_type == "gateway"
    assert rows[0].payload["skip_deps"] is True
    assert rows[0].payload["from_version"]


def test_a_second_press_while_one_is_queued_is_not_recorded_as_a_second_update(
    client, monkeypatch, tmp_path
):
    from app.core import release

    monkeypatch.setattr(release, "updates_are_wired", lambda: True)
    monkeypatch.setattr(release, "state_dir", lambda: tmp_path)
    admin(client, "POST", "/admin/version/update", json={})

    again = admin(client, "POST", "/admin/version/update", json={})

    assert again.json()["accepted"] is False
    assert len(audit(client, "version.update")) == 1


def test_an_install_without_the_update_mechanism_records_nothing(client, monkeypatch, tmp_path):
    from app.core import release

    monkeypatch.setattr(release, "updates_are_wired", lambda: False)
    monkeypatch.setattr(release, "state_dir", lambda: tmp_path)
    assert admin(client, "POST", "/admin/version/update", json={}).status_code == 400
    assert audit(client, "version.update") == []
    assert list(tmp_path.iterdir()) == []


# ── ตัวช่วย ───────────────────────────────────────────────────────────────────

def _admin_id(client) -> str:
    return next(u["id"] for u in admin(client, "GET", "/admin/users").json()["data"]
                if u["external_id"] == "test-admin")


def _assert_no_secret(client, row: AuditLog, plaintext: str) -> None:
    """ตาราง audit อ่านได้โดยผู้ดูแลทุกคนและเก็บนานกว่า usage — ต้องไม่มีทั้งตัว key และ hash"""
    from app.core.auth import hash_api_key

    written = f"{row.payload} {row.target_id} {row.target_type} {row.action}"
    assert plaintext not in written
    assert hash_api_key(plaintext) not in written
    # ส่วนที่เป็นความลับคือทุกอย่างหลัง prefix
    assert plaintext[len(row.payload.get("key_prefix", "") or "lg_sk_"):] not in written


def test_the_cookie_name_this_file_signs_in_with_is_the_plain_http_one(client):
    """ถ้า TestClient เปลี่ยนไปใช้ https เทสข้างบนจะลงชื่อเข้าใช้ไม่ติดโดยไม่มีอะไรบอก"""
    person(client)
    assert SESSION_COOKIE_INSECURE in sign_in(client).cookies


@pytest.mark.parametrize("body", [[], "skip", 7])
def test_an_update_request_with_a_body_that_is_not_an_object_is_not_a_500(
    client, monkeypatch, tmp_path, body
):
    from app.core import release

    monkeypatch.setattr(release, "updates_are_wired", lambda: True)
    monkeypatch.setattr(release, "state_dir", lambda: tmp_path)
    asked = admin(client, "POST", "/admin/version/update", json=body)
    assert asked.status_code == 200, asked.text
    assert audit(client, "version.update")[0].payload["skip_deps"] is False
