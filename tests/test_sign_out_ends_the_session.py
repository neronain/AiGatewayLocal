"""กด sign out แล้ว session ที่ใช้อยู่ต้องใช้ต่อไม่ได้ — ไม่ใช่แค่คุกกี้หายจากเบราว์เซอร์

เคสจริง (ตรวจ 2026-10-06 บน a5e9295): ผู้ดูแลลงชื่อเข้าใช้ คัดลอกค่าคุกกี้ไว้ กด sign out
(200 `signed_out: true`) แล้วส่งคุกกี้ใบเดิมไปที่ `/admin/users` — ได้ 200 · logout ทำแค่
สั่งเบราว์เซอร์ให้ลบคุกกี้ ตัว token ข้างในยังใช้ได้จนหมดอายุเองอีกถึงแปดชั่วโมง

session เป็น token ที่เซ็นแล้วถือ `session_epoch` ของผู้ใช้ ไม่มีตาราง session ให้ลบทีละ
ใบ · ทางเดียวที่ปลด token ได้คือขยับ epoch ซึ่งปลด **ทุก** session ของบัญชีนั้นบนทุก
เครื่อง (เหมือนตอนเปลี่ยนรหัสผ่าน) — คำตอบและแถว audit จึงต้องบอกตรง ๆ ว่าเป็นแบบนั้น
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.passwords import SESSION_COOKIE_INSECURE, hash_password
from app.db.models import AuditLog, User
from app.db.session import session_scope

PASSWORD = "correct horse battery"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def with_cookie(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_INSECURE}={value}"}


def give_password(client, external_id="admin") -> str:
    async def run():
        async with session_scope() as session:
            user = (await session.execute(
                select(User).where(User.external_id == external_id)
            )).scalar_one()
            user.password_hash = hash_password(PASSWORD)
            return user.id

    return client.portal.call(run)


def sign_in(client, username="admin") -> str:
    """ลงชื่อเข้าใช้แล้วคืนค่าคุกกี้ — และล้างกระเป๋าคุกกี้ของ client เพื่อให้เทสส่งเองทุกครั้ง"""
    client.cookies.clear()
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    cookie = response.cookies.get(SESSION_COOKIE_INSECURE)
    client.cookies.clear()
    return cookie


def logout_rows(client) -> list[AuditLog]:
    async def read():
        async with session_scope() as session:
            return list((await session.execute(
                select(AuditLog).where(AuditLog.action == "auth.logout")
            )).scalars())

    return client.portal.call(read)


def test_a_copied_cookie_stops_working_after_sign_out(client):
    give_password(client)
    cookie = sign_in(client)
    assert client.get("/admin/users", headers=with_cookie(cookie)).status_code == 200

    out = client.post("/auth/logout", headers=with_cookie(cookie))
    assert out.status_code == 200

    assert client.get("/admin/users", headers=with_cookie(cookie)).status_code == 401
    assert client.get("/v1/me", headers=with_cookie(cookie)).status_code == 401
    assert client.get("/auth/status", headers=with_cookie(cookie)).json()["session"] is None


def test_it_signs_the_account_out_everywhere_and_says_so(client):
    """ไม่มีตาราง session — ปลดได้ทีละบัญชี ไม่ใช่ทีละเครื่อง · ต้องบอก ไม่ใช่ปล่อยให้เดา"""
    user_id = give_password(client)
    laptop, phone = sign_in(client), sign_in(client)

    out = client.post("/auth/logout", headers=with_cookie(laptop))

    assert out.json() == {"signed_out": True, "all_sessions_signed_out": True}
    assert client.get("/v1/me", headers=with_cookie(phone)).status_code == 401
    rows = logout_rows(client)
    assert len(rows) == 1
    assert rows[0].actor_user_id == user_id
    assert (rows[0].target_type, rows[0].target_id) == ("user", user_id)
    assert rows[0].payload == {"all_sessions_signed_out": True}


def test_signing_in_again_works_straight_away(client):
    give_password(client)
    client.post("/auth/logout", headers=with_cookie(sign_in(client)))
    assert client.get("/v1/me", headers=with_cookie(sign_in(client))).status_code == 200


def test_api_keys_are_not_sessions_and_keep_working(client):
    give_password(client, "test-admin")
    client.post("/auth/logout", headers=with_cookie(sign_in(client, "test-admin")))
    assert client.get("/admin/users", headers=auth(client.admin_key)).status_code == 200


def test_other_peoples_sessions_are_untouched(client):
    give_password(client)
    give_password(client, "test-admin")
    mine, theirs = sign_in(client), sign_in(client, "test-admin")

    client.post("/auth/logout", headers=with_cookie(mine))

    assert client.get("/v1/me", headers=with_cookie(theirs)).status_code == 200


def test_a_cookie_that_was_already_retired_cannot_sign_anyone_out(client):
    """ไม่งั้น token ที่ถูกปลดไปแล้วจะกลายเป็นปุ่มเตะเจ้าของออกจากระบบได้เรื่อย ๆ"""
    give_password(client)
    old = sign_in(client)
    client.post("/auth/logout", headers=with_cookie(old))
    fresh = sign_in(client)

    again = client.post("/auth/logout", headers=with_cookie(old))

    assert again.json() == {"signed_out": True, "all_sessions_signed_out": False}
    assert client.get("/v1/me", headers=with_cookie(fresh)).status_code == 200
    assert len(logout_rows(client)) == 1


def test_signing_out_without_a_session_is_harmless(client):
    for headers in ({}, with_cookie("not-a-token"), with_cookie("a.b")):
        out = client.post("/auth/logout", headers=headers)
        assert out.status_code == 200
        assert out.json() == {"signed_out": True, "all_sessions_signed_out": False}
    assert logout_rows(client) == []


def test_the_browser_is_still_told_to_drop_both_cookies(client):
    give_password(client)
    out = client.post("/auth/logout", headers=with_cookie(sign_in(client)))
    cleared = out.headers.get_list("set-cookie")
    assert any(c.startswith("litegate_session=") for c in cleared)
    assert any(c.startswith("litegate_session_http=") for c in cleared)
