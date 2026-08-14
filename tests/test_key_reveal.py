"""Reading back a key that was already issued, at a cost that is stated.

A key is stored as a digest, so "show me that key again" normally has no answer.
That is the right default and it stays the default. But somebody loses the key
they were handed, and the only remedy is a replacement — which means finding
every config file and CI secret that held the old one. For thirty people that is
a morning's work caused by one mislaid note.

So a sealed second copy can be kept and an administrator can open it. Everything
here exists to keep that narrow: off unless switched on, administrators only,
never for a revoked key, and every opening recorded.
"""

from __future__ import annotations

import pytest

SECRET = "test-reveal-secret-not-a-real-one"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def sealed(monkeypatch):
    """เปิดใช้การเรียกดู — ต้องขอ fixture นี้ *ก่อน* client เสมอ

    `get_settings` แคชไว้ทั้งกระบวนการ ตั้ง env หลังจาก client สร้างแล้วจะไม่มีผล
    และเทสจะผ่านด้วยเหตุผลผิด ๆ
    """
    from app import config as config_mod

    monkeypatch.setenv("GW_KEY_REVEAL_SECRET", SECRET)
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


def person(client, external_id="s1"):
    return client.post("/admin/users", headers=auth(client.admin_key),
                       json={"external_id": external_id}).json()


def issue(client, who=None, **extra):
    who = who or person(client)
    return client.post("/admin/api-keys", headers=auth(client.admin_key),
                       json={"user_id": who["id"], "name": "k", **extra}).json()


def reveal(client, key_id, as_key=None):
    return client.post(f"/admin/api-keys/{key_id}/reveal",
                       headers=auth(as_key or client.admin_key))


# ── ปิดอยู่เป็นค่าตั้งต้น ────────────────────────────────────────────────────

def test_nothing_is_kept_unless_the_operator_asked_for_it(client):
    """ไม่ได้ตั้ง secret = เก็บแค่ hash เหมือนเดิม · การลดความปลอดภัยต้องเป็นการกระทำ
    ของคน ไม่ใช่สิ่งที่เกิดขึ้นเอง"""
    created = issue(client)
    assert reveal(client, created["id"]).status_code >= 400

    listed = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    assert all(k["revealable"] is False for k in listed)


def test_the_list_says_which_keys_can_be_opened(sealed, client):
    """หน้าเว็บต้องรู้ก่อนวาดปุ่ม ไม่ใช่ให้กดแล้วค่อยบอกว่าทำไม่ได้"""
    created = issue(client)
    listed = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    assert next(k for k in listed if k["id"] == created["id"])["revealable"] is True


# ── เปิดดูแล้วได้ของจริง ────────────────────────────────────────────────────

def test_what_comes_back_is_the_key_that_was_issued(sealed, client):
    created = issue(client)
    assert reveal(client, created["id"]).json()["api_key"] == created["api_key"]


def test_the_revealed_key_actually_works(sealed, client):
    """เทียบสตริงตรงกันยังไม่พอ — ของที่คืนมาต้องใช้เรียก API ได้จริง"""
    created = issue(client)
    recovered = reveal(client, created["id"]).json()["api_key"]
    assert client.get("/v1/models", headers=auth(recovered)).status_code == 200


# ── ใครเปิดได้บ้าง ──────────────────────────────────────────────────────────

def test_a_manager_cannot_read_the_secrets_of_people_they_manage(sealed, client):
    """manager ออก key ให้คนในกลุ่มตัวเองได้อยู่แล้ว · การอ่านความลับที่ออกไปแล้ว
    เป็นคนละอำนาจ — ดูแลคนไม่ได้แปลว่าต้องอ่านความลับของเขา"""
    lecturer = person(client, "lecturer")
    client.patch(f"/admin/users/{lecturer['id']}", headers=auth(client.admin_key),
                 json={"role": "manager"})
    their_key = issue(client, lecturer)["api_key"]

    target = issue(client, person(client, "student"))
    assert reveal(client, target["id"], as_key=their_key).status_code in (401, 403)


def test_a_member_cannot_reveal_anything(sealed, client, member_key):
    target = issue(client)
    assert reveal(client, target["id"], as_key=member_key).status_code in (401, 403)


def test_a_member_cannot_even_reveal_their_own_key(sealed, client, member_key):
    """ของตัวเองก็ไม่ได้ · ถ้าอ่านคืนได้เอง key ที่หลุดไปจะถูกกู้กลับโดยคนที่ขโมยมัน"""
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    me = next(u for u in users if u["role"] == "member")
    mine = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    own = next(k for k in mine if k["user_id"] == me["id"])
    assert reveal(client, own["id"], as_key=member_key).status_code in (401, 403)


# ── ขอบเขตที่ต้องไม่ข้าม ────────────────────────────────────────────────────

def test_a_revoked_key_is_never_revealed(sealed, client):
    """เพิกถอนคือการตัดสินใจที่ตั้งใจให้ย้อนกลับไม่ได้ — การเปิดดูต้องไม่กลายเป็นทางกลับ"""
    created = issue(client)
    client.delete(f"/admin/api-keys/{created['id']}", headers=auth(client.admin_key))
    assert reveal(client, created["id"]).status_code == 400


def test_a_key_from_before_the_feature_says_so_rather_than_failing_oddly(client, monkeypatch):
    """ออก key ตอนปิดอยู่ แล้วเปิดฟีเจอร์ทีหลัง — ใบนั้นไม่มีอะไรให้เปิด ตลอดไป"""
    from app import config as config_mod

    created = issue(client)                       # ยังไม่ได้ตั้ง secret
    monkeypatch.setenv("GW_KEY_REVEAL_SECRET", SECRET)
    config_mod.get_settings.cache_clear()
    try:
        response = reveal(client, created["id"])
        assert response.status_code == 400
        assert "hash" in response.json()["error"]["message"].lower()
    finally:
        config_mod.get_settings.cache_clear()


def test_changing_the_secret_locks_the_old_keys_rather_than_crashing(sealed, client, monkeypatch):
    from app import config as config_mod

    created = issue(client)
    monkeypatch.setenv("GW_KEY_REVEAL_SECRET", "a-different-secret-entirely")
    config_mod.get_settings.cache_clear()
    try:
        assert reveal(client, created["id"]).status_code == 400
    finally:
        config_mod.get_settings.cache_clear()


def test_an_unknown_key_id_is_refused(sealed, client):
    assert reveal(client, "does-not-exist").status_code >= 400


# ── ทุกครั้งถูกบันทึก ────────────────────────────────────────────────────────

def test_every_opening_is_recorded_and_readable(sealed, client):
    """การบันทึกที่ไม่มีใครอ่านได้ไม่ใช่การบันทึก"""
    created = issue(client)
    reveal(client, created["id"])
    reveal(client, created["id"])

    history = client.get(f"/admin/api-keys/{created['id']}/reveals",
                         headers=auth(client.admin_key)).json()["data"]
    assert len(history) == 2
    assert all(row["at"] and row["by"] for row in history)


def test_a_refused_attempt_leaves_no_reveal_in_the_log(sealed, client):
    """บันทึกก่อนคืนค่า · ครั้งที่ถูกปฏิเสธต้องไม่ปรากฏเป็นการเปิดดูที่สำเร็จ"""
    created = issue(client)
    client.delete(f"/admin/api-keys/{created['id']}", headers=auth(client.admin_key))
    reveal(client, created["id"])

    history = client.get(f"/admin/api-keys/{created['id']}/reveals",
                         headers=auth(client.admin_key)).json()["data"]
    assert history == []


def test_only_an_admin_can_read_the_reveal_history(sealed, client, member_key):
    created = issue(client)
    response = client.get(f"/admin/api-keys/{created['id']}/reveals", headers=auth(member_key))
    assert response.status_code in (401, 403)


# ── การผนึกเอง ──────────────────────────────────────────────────────────────

def test_sealing_is_not_reversible_without_the_secret(sealed):
    from app import config as config_mod
    from app.core.keyvault import seal, unseal

    blob = seal("lg_sk_something")
    assert "lg_sk_something" not in blob, "ของที่ผนึกแล้วต้องไม่มีต้นฉบับอยู่ในนั้น"
    assert unseal(blob) == "lg_sk_something"

    config_mod.get_settings.cache_clear()   # secret หายไป = เปิดไม่ได้
    import os

    del os.environ["GW_KEY_REVEAL_SECRET"]
    config_mod.get_settings.cache_clear()
    assert unseal(blob) is None


def test_two_seals_of_the_same_value_are_not_identical(sealed):
    """nonce ต้องต่างกันทุกครั้ง ไม่งั้นดูจากฐานข้อมูลก็รู้ว่าใครใช้ key เดียวกัน"""
    from app.core.keyvault import seal

    assert seal("lg_sk_same") != seal("lg_sk_same")


# ── คำเตือนบนหน้าเว็บต้องเป็นจริงตามการตั้งค่า ──────────────────────────────

def test_issuing_says_whether_the_key_can_be_read_back(sealed, client):
    """หน้าเว็บเคยขึ้นว่า "เก็บไว้เดี๋ยวนี้ เรียกดูอีกไม่ได้" ทุกกรณี · ปล่อยให้พูดแบบนั้น
    ทั้งที่เรียกดูได้ คือสอนให้คนเลิกเชื่อคำเตือนของระบบ"""
    assert issue(client)["revealable"] is True


def test_issuing_with_the_feature_off_says_it_cannot_be_read_back(client):
    assert issue(client)["revealable"] is False


def test_the_console_only_draws_the_button_for_an_admin():
    """ปุ่มที่ทุกคนเห็นแล้วกดไม่ได้คือปุ่มที่ทำให้คนเข้าใจสิทธิ์ของตัวเองผิด"""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text()
    assert "k.revealable && myRole === 'admin'" in page
