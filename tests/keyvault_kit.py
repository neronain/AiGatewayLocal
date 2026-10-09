"""ของใช้ร่วมของเทสเรื่องการเปลี่ยน secret ที่ผนึกสำเนา API key

แยกออกมาเพราะสามไฟล์ (`test_key_rotation` · `test_key_vault_status` · `test_key_reseal`)
ต้องสร้างสถานการณ์เดียวกัน: ของที่ผนึกด้วย secret เก่า ทั้งแบบที่รุ่นที่ deploy อยู่เขียน
(`v1`) และแบบที่รุ่นนี้เขียน · ถ้าแต่ละไฟล์เขียนเองจะเหลื่อมกันแล้วเทสผ่านด้วยเหตุผลคนละอย่าง

secret ในไฟล์นี้เป็นค่าปลอมสำหรับเทสเท่านั้น
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

A = "rotation-test-secret-A-not-real"
B = "rotation-test-secret-B-not-real"
C = "rotation-test-secret-C-not-real"

# ค่าที่โค้ดรุ่น 1.12.1 (commit c1876db — ตัวที่ deploy อยู่ตอนเขียนเทสนี้) ผนึกออกมาจริง
# ด้วย secret A · เก็บเป็นค่าตายตัวเพราะคำถามคือ "ของที่เขียนลงฐานไปแล้วยังเปิดได้ไหม"
# ซึ่งตอบไม่ได้ด้วยการให้โค้ดรุ่นใหม่ผนึกเองแล้วเปิดเอง
GOLDEN_V1 = (
    "v1:PO7LmsCp7_6w33-GUh58N-9fuPq3J6"
    "sjt6Zw_6UTxRsdCQwsrCKCqmKby3KroXSh"
)
# สั้นกว่ารูปของ key จริงโดยตั้งใจ: ตัวสแกนความลับของ CI (.github/workflows/ci.yml) จับ `lg_sk_` ที่ตามด้วย
# 20 ตัวขึ้นไปทั้ง tree รวม tests/ — ค่าเดิมยาว 29 ตัว จะทำให้ main แดงทันทีที่ push (ทีม git จับได้ 2026-10-09)
# ผนึกใหม่ด้วยโค้ดของ c1876db จริง ไม่ได้หั่นสตริงหลบตัวสแกน
GOLDEN_PLAINTEXT = "lg_sk_golden_fixture"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def sealed_by_deployed_version(plaintext: str, secret: str) -> str:
    """ผนึกแบบที่รุ่น 1.12.1 เขียนลงฐาน — เขียนซ้ำไว้ที่นี่โดยตั้งใจ ไม่เรียกโค้ดของแอป

    ถ้าเรียก `keyvault` ให้ผนึก เทสจะได้ของรูปแบบใหม่เสมอ แล้วไม่มีอะไรตรวจว่าแถวที่
    อยู่ในฐานจริงตอนนี้ (ซึ่งเป็น `v1` ทั้งหมด) ผ่านการเปลี่ยน secret ไปได้
    `test_the_kit_writes_what_the_deployed_version_wrote` ยืนยันว่าสูตรนี้ตรงกับ GOLDEN_V1
    """
    key = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"litegate-key-reveal", 200_000)
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return "v1:" + base64.urlsafe_b64encode(blob).decode()


def secret_switch(monkeypatch):
    """ตัวตั้งและเปลี่ยน secret กลางเทส — แต่ละไฟล์ห่อเป็น fixture ชื่อ `secrets` ของตัวเอง

    ต้องขอ fixture นั้น *ก่อน* client เสมอ · ให้ฟังก์ชัน `use(current, previous=None)`
    โดย None = ไม่ตั้งตัวแปรนั้นเลย · `get_settings` แคชไว้ทั้งกระบวนการ จึงล้างทุกครั้งที่
    เปลี่ยน ไม่งั้นเทสจะยังเห็นค่าเก่าแล้วผ่านด้วยเหตุผลผิด
    """
    from app import config as config_mod

    def use(current: str | None = None, previous: str | None = None) -> None:
        for name, value in (
            ("GW_KEY_REVEAL_SECRET", current),
            ("GW_KEY_REVEAL_SECRET_PREVIOUS", previous),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        config_mod.get_settings.cache_clear()

    use()
    yield use
    config_mod.get_settings.cache_clear()


# ── ทาง admin API ────────────────────────────────────────────────────────────

def person(client, external_id="s1"):
    return client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id}).json()


def issue(client, who=None, name="k"):
    who = who or person(client)
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": who["id"], "name": name}).json()


def issue_many(client, count: int, prefix="k"):
    who = person(client, f"owner-{prefix}")
    return [issue(client, who, name=f"{prefix}{i}") for i in range(count)]


def reveal(client, key_id, as_key=None):
    return client.post(f"/admin/api-keys/{key_id}/reveal",
                       headers=auth(as_key or client.admin_key))


def listed(client) -> dict[str, dict]:
    rows = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    return {row["id"]: row for row in rows}


def vault(client, as_key=None):
    return client.get("/admin/key-vault", headers=auth(as_key or client.admin_key))


def reseal(client, as_key=None):
    return client.post("/admin/key-vault/reseal", headers=auth(as_key or client.admin_key))


def opens(client, created) -> bool:
    """เปิดดูผ่าน route จริงแล้วได้ key ใบที่ออกไปจริง"""
    response = reveal(client, created["id"])
    return response.status_code == 200 and response.json()["api_key"] == created["api_key"]


# ── ทางฐานข้อมูลตรง ๆ — ดูและวางของที่ route ไม่มีให้ ──────────────────────────

def sealed_column(client) -> dict[str, str]:
    """ค่าที่ผนึกไว้ของทุกแถว ตามที่อยู่ในฐานจริง"""
    from sqlalchemy import select

    from app.db.models import ApiKey
    from app.db.session import session_scope

    async def read():
        async with session_scope() as session:
            rows = (await session.execute(select(ApiKey.id, ApiKey.key_sealed))).all()
            return {row[0]: row[1] or "" for row in rows}

    return client.portal.call(read)


def put_sealed(client, key_id: str, value: str) -> None:
    from sqlalchemy import update

    from app.db.models import ApiKey
    from app.db.session import session_scope

    async def write():
        async with session_scope() as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(key_sealed=value)
            )

    client.portal.call(write)


def as_deployed_version_wrote(client, created, secret: str) -> None:
    """ทำให้แถวนี้เป็นอย่างที่รุ่น 1.12.1 จะเขียน — `v1` ไม่มีป้ายบอกว่า secret ไหนผนึก"""
    put_sealed(client, created["id"], sealed_by_deployed_version(created["api_key"], secret))


def issued_under(client, secret_fixture, secret: str, count: int, *, prefix: str, fmt: str):
    """ออก key `count` ใบขณะที่ secret ปัจจุบันคือ `secret` · fmt = "v1" หรือ "new" """
    secret_fixture(secret)
    keys = issue_many(client, count, prefix=prefix)
    if fmt == "v1":
        for created in keys:
            as_deployed_version_wrote(client, created, secret)
    return keys
