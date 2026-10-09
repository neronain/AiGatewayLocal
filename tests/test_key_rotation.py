"""เปลี่ยน secret ที่ผนึกสำเนา API key ได้ โดยของที่ผนึกไว้แล้วไม่หาย

ก่อนหน้านี้ `GW_KEY_REVEAL_SECRET` เปลี่ยนไม่ได้เลยในทางปฏิบัติ: เปลี่ยนปุ๊บสำเนาทุกใบที่
ผนึกไว้เปิดไม่ออก และไม่มีอะไรบอกจนกว่าจะมีคนกด Reveal แล้วได้ข้อความว่า "เก็บแค่ hash"
— ซึ่งไม่จริง ของยังอยู่ครบ แค่กุญแจไม่ใช่ดอกเดิม · secret ที่เปลี่ยนไม่ได้คือ secret ที่
หลุดแล้วทำอะไรไม่ได้

ไฟล์นี้ดูสามเรื่อง: ของเก่าเปิดได้ระหว่างเปลี่ยน · แต่ละใบบอกได้ว่าอยู่สถานะไหน ·
ใบที่เปิดไม่ได้จริง ๆ บอกเหตุและทางออก ไม่ใช่ข้อความที่ชี้ไปผิดทาง

ทุกข้อผนึกและเปิดด้วย AES-GCM จริงผ่านโมดูลและ route จริง ไม่มีการ mock ตัวเข้ารหัส
"""

from __future__ import annotations

import pytest

from tests.keyvault_kit import (
    GOLDEN_PLAINTEXT,
    GOLDEN_V1,
    A,
    B,
    C,
    as_deployed_version_wrote,
    auth,
    issue,
    issued_under,
    listed,
    opens,
    put_sealed,
    reveal,
    sealed_by_deployed_version,
    sealed_column,
    secret_switch,
)

FORMATS = pytest.mark.parametrize("fmt", ["v1", "new"])


@pytest.fixture
def secrets(monkeypatch):
    yield from secret_switch(monkeypatch)


# ── ของที่รุ่นที่ deploy อยู่เขียนไว้ ───────────────────────────────────────

def test_a_value_the_deployed_version_wrote_still_opens(secrets):
    """ยามกันถอยหลัง: แถวในฐานจริงตอนนี้เป็น `v1` ทั้งหมด — ต้องเปิดได้ต่อไป"""
    from app.core.keyvault import unseal

    secrets(A)
    assert unseal(GOLDEN_V1) == GOLDEN_PLAINTEXT


def test_the_kit_writes_what_the_deployed_version_wrote(secrets):
    """สูตร `v1` ที่เทสใช้วางแถวเก่าต้องเป็นสูตรเดียวกับของจริง ไม่ใช่ของที่เทสคิดเอง

    เปิด GOLDEN_V1 (ของที่รุ่น 1.12.1 ผนึกออกมาจริง) ด้วยสูตรในชุดเทสได้ และของที่ชุดเทส
    ผนึกก็เปิดได้ด้วยโค้ดของแอป — สองทางตรงกันจึงเป็นรูปแบบเดียวกัน
    """
    import base64
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.core.keyvault import unseal

    key = hashlib.pbkdf2_hmac("sha256", A.encode(), b"litegate-key-reveal", 200_000)
    blob = base64.urlsafe_b64decode(GOLDEN_V1.split(":", 1)[1])
    assert AESGCM(key).decrypt(blob[:12], blob[12:], None).decode() == GOLDEN_PLAINTEXT

    secrets(A)
    assert unseal(sealed_by_deployed_version("lg_sk_from_the_kit", A)) == "lg_sk_from_the_kit"


# ── secret เก่ายังเปิดของเก่าได้ ────────────────────────────────────────────

def test_the_old_secret_named_as_previous_still_opens_what_it_sealed(secrets):
    """หัวใจของทั้งเรื่อง: current=B, previous=A แล้วของที่ A ผนึกต้องยังเปิดได้"""
    from app.core.keyvault import seal, unseal

    secrets(A)
    from_this_version = seal("lg_sk_sealed_under_a")
    secrets(B, previous=A)

    assert unseal(GOLDEN_V1) == GOLDEN_PLAINTEXT, "ของที่รุ่นเก่าเขียน"
    assert unseal(from_this_version) == "lg_sk_sealed_under_a", "ของที่รุ่นนี้เขียน"


def test_new_values_are_sealed_under_the_current_secret_not_the_previous(secrets):
    """ระหว่างเปลี่ยน ของใหม่ต้องไปอยู่ใต้ตัวใหม่ทันที ไม่งั้นงานผนึกใหม่ไม่มีวันจบ"""
    from app.core.keyvault import seal, unseal

    secrets(B, previous=A)
    blob = seal("lg_sk_issued_mid_rotation")
    secrets(B)                                  # เอา previous ออก
    assert unseal(blob) == "lg_sk_issued_mid_rotation"
    secrets(A)                                  # ตัวเก่าตัวเดียวต้องเปิดไม่ได้
    assert unseal(blob) is None


def test_the_previous_secret_alone_does_not_switch_the_feature_on(secrets):
    """previous มีไว้เปลี่ยนผ่านเท่านั้น · ตั้งตัวเดียวต้องไม่กลายเป็นการเปิดเก็บสำเนา

    การเก็บสำเนาคือท่าทีความปลอดภัยที่อ่อนลง ต้องเป็นสิ่งที่คนตั้งใจเปิดด้วยตัวแปรหลัก
    ไม่ใช่ผลข้างเคียงของตัวแปรที่ลืมเอาออก
    """
    from app.core.keyvault import reveal_enabled, seal, unseal

    secrets(None, previous=A)
    assert reveal_enabled() is False
    assert seal("lg_sk_anything") == ""
    assert unseal(GOLDEN_V1) is None


@FORMATS
def test_reveal_keeps_working_when_the_secret_is_changed_properly(secrets, client, fmt):
    """ทาง route จริง: ออกใต้ A → ตั้ง current=B previous=A → ผู้ดูแลยังเปิดดูได้"""
    (created,) = issued_under(client, secrets, A, 1, prefix="old", fmt=fmt)
    secrets(B, previous=A)

    response = reveal(client, created["id"])
    assert response.status_code == 200, response.text
    assert response.json()["api_key"] == created["api_key"]


# ── แต่ละใบบอกสถานะของตัวเอง ────────────────────────────────────────────────

@FORMATS
def test_the_list_tells_the_three_states_apart(secrets, client, fmt):
    """เปิดได้ด้วยตัวปัจจุบัน · เปิดได้ด้วยตัวเก่าเท่านั้น (รอผนึกใหม่) · เปิดไม่ได้เลย

    เดิมทุกใบที่มีสำเนาขึ้น `revealable: true` ตราบที่ตั้ง secret อะไรไว้สักตัว —
    หน้าเว็บวาดปุ่ม Reveal ให้ใบที่กดแล้วล้มแน่ ๆ
    """
    secrets()
    bare = issue(client, name="before-the-feature")          # ไม่มีสำเนาเลย
    (lost,) = issued_under(client, secrets, C, 1, prefix="lost", fmt=fmt)
    (pending,) = issued_under(client, secrets, A, 1, prefix="pending", fmt=fmt)
    (fresh,) = issued_under(client, secrets, B, 1, prefix="fresh", fmt=fmt)
    secrets(B, previous=A)

    rows = listed(client)
    assert rows[fresh["id"]]["seal_state"] == "current"
    assert rows[pending["id"]]["seal_state"] == "previous"
    assert rows[lost["id"]]["seal_state"] == "lost"
    assert rows[bare["id"]]["seal_state"] == "none"
    # ปุ่ม Reveal ขึ้นกับ `revealable` — ต้องจริงเฉพาะใบที่กดแล้วได้ของ
    assert rows[fresh["id"]]["revealable"] is True
    assert rows[pending["id"]]["revealable"] is True
    assert rows[lost["id"]]["revealable"] is False
    assert rows[bare["id"]]["revealable"] is False


def test_a_sealed_copy_with_the_feature_switched_off_is_not_called_lost(secrets, client):
    """ปิดฟีเจอร์ (ไม่ตั้ง secret) ≠ ของหาย · ตั้งกลับแล้วเปิดได้ จึงต้องไม่บอกให้ออกใบใหม่"""
    (created,) = issued_under(client, secrets, A, 1, prefix="x", fmt="new")
    secrets()

    row = listed(client)[created["id"]]
    assert row["seal_state"] == "off"
    assert row["revealable"] is False


# ── ใบที่เปิดไม่ได้จริง ๆ ───────────────────────────────────────────────────

@FORMATS
def test_revealing_a_lost_copy_says_what_happened_and_what_to_do(secrets, client, fmt):
    """ผนึกด้วย secret ที่ระบบไม่รู้จักแล้ว — ต้องตอบในซองข้อผิดพลาดเดิม ไม่ใช่ 500

    ข้อความเดิมบอกว่า "เก็บแค่ hash … ออกก่อนเปิดฟีเจอร์ หรือไม่ได้ตั้ง secret" ซึ่งผิดทั้งสอง
    ข้อในเคสนี้ ผู้ดูแลที่เชื่อข้อความนั้นจะไม่มีทางรู้ว่าเอา secret เก่ากลับมาแล้วเปิดได้
    """
    (created,) = issued_under(client, secrets, A, 1, prefix="x", fmt=fmt)
    secrets(C)

    response = reveal(client, created["id"])
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["details"]["seal_state"] == "lost"
    message = error["message"]
    assert "GW_KEY_REVEAL_SECRET_PREVIOUS" in message, "ทางกู้: เอา secret เดิมกลับมาเป็น previous"
    assert "new key" in message.lower(), "ทางออกเมื่อ secret เดิมไม่อยู่แล้ว: ออกใบใหม่"
    assert "hash" not in message.lower(), "สำเนายังอยู่ — ห้ามบอกว่าเก็บแค่ hash"
    assert created["api_key"] not in response.text


def test_a_lost_copy_does_not_stop_the_key_itself_from_working(secrets, client):
    """สำเนาที่เปิดไม่ได้กระทบแค่การเรียกดูซ้ำ · key ยืนยันตัวด้วย hash ไม่ได้ใช้สำเนา"""
    (created,) = issued_under(client, secrets, A, 1, prefix="x", fmt="new")
    secrets(C)

    assert listed(client)[created["id"]]["seal_state"] == "lost"
    assert client.get("/v1/models", headers=auth(created["api_key"])).status_code == 200


def test_a_lost_reveal_is_not_recorded_as_a_reveal(secrets, client):
    """ครั้งที่ไม่ได้ของต้องไม่ปรากฏในประวัติว่ามีคนเปิดดู"""
    (created,) = issued_under(client, secrets, A, 1, prefix="x", fmt="new")
    secrets(C)
    reveal(client, created["id"])

    history = client.get(f"/admin/api-keys/{created['id']}/reveals",
                         headers=auth(client.admin_key)).json()["data"]
    assert history == []


def test_a_damaged_copy_is_told_apart_from_a_wrong_secret(secrets, client):
    """ป้ายบอกว่า secret ปัจจุบันผนึก แต่เนื้อเปิดไม่ออก = ข้อมูลเสีย · เอา secret ไหนมาก็ไม่ช่วย

    แยกสองเหตุนี้ได้เพราะค่าที่ผนึกจดไว้ว่า secret ตัวไหนผนึก — บอกให้ผู้ดูแลไปตาม secret
    เก่าทั้งที่ตัวที่มีอยู่ก็ถูกแล้ว คือส่งเขาไปหาของที่ไม่มี
    """
    (created,) = issued_under(client, secrets, A, 1, prefix="x", fmt="new")
    stored = sealed_column(client)[created["id"]]
    head, _, payload = stored.rpartition(":")
    flipped = ("A" if payload[20] != "A" else "B")
    put_sealed(client, created["id"], f"{head}:{payload[:20]}{flipped}{payload[21:]}")

    assert listed(client)[created["id"]]["seal_state"] == "lost"
    error = reveal(client, created["id"]).json()["error"]
    assert error["details"]["seal_state"] == "lost"
    assert error["details"]["reason"] == "damaged"
    assert "damaged" in error["message"].lower()


# ── ค่าที่ผนึกจดว่า secret ไหนผนึก โดยไม่เผย secret ─────────────────────────

def test_what_is_stored_names_its_secret_without_containing_it(secrets):
    """ป้ายในค่าที่ผนึกต้องต่างกันตาม secret คงที่สำหรับ secret เดิม และไม่ใช่ตัว secret"""
    from app.core.keyvault import seal

    secrets(A)
    first, second = seal("lg_sk_one"), seal("lg_sk_two")
    secrets(B)
    other = seal("lg_sk_one")

    label = lambda blob: blob.split(":")[1]            # noqa: E731
    assert first.startswith("v2:") and other.startswith("v2:")
    assert label(first) == label(second), "secret เดิม = ป้ายเดิม"
    assert label(first) != label(other), "secret ต่าง = ป้ายต่าง"
    for blob in (first, second, other):
        assert A not in blob and B not in blob
        assert "lg_sk_one" not in blob and "lg_sk_two" not in blob


def test_relabelling_a_stored_value_does_not_make_it_open(secrets):
    """ป้ายถูกผูกกับเนื้อที่เข้ารหัส · แก้ป้ายในฐานให้ชี้ secret อื่นแล้วต้องเปิดไม่ออก"""
    from app.core.keyvault import seal, unseal

    secrets(A)
    under_a = seal("lg_sk_one")
    secrets(B)
    label_b = seal("lg_sk_x").split(":")[1]
    _, _, payload = under_a.split(":")

    secrets(A, previous=B)
    assert unseal(f"v2:{label_b}:{payload}") is None


def test_a_value_from_a_newer_format_is_refused_not_guessed(secrets):
    """ถอยรุ่นลงมาแล้วเจอของที่รุ่นใหม่กว่าเขียน — บอกว่าเปิดไม่ได้ ไม่เดารูปแบบ"""
    from app.core.keyvault import unseal

    secrets(A)
    assert unseal("v9:whatever:AAAA") is None
    assert unseal("not-a-sealed-value") is None


# ── เดินทั้งเส้น: ไม่มีจังหวะไหนที่ของที่เคยเปิดได้กลายเป็นเปิดไม่ได้ ─────────

def test_the_deployed_database_survives_the_upgrade_untouched(secrets, client):
    """อัปเกรดอย่างเดียว ไม่เปลี่ยน secret: แถว `v1` ต้องเปิดได้และ **ไม่ถูกเขียนทับ**

    ถอยกลับไปรุ่นเก่าต้องยังอ่านแถวเดิมออก — รุ่นเก่าไม่รู้จักรูปแบบใหม่
    """
    keys = issued_under(client, secrets, A, 3, prefix="old", fmt="v1")
    before = sealed_column(client)

    assert all(opens(client, created) for created in keys)
    assert all(listed(client)[k["id"]]["seal_state"] == "current" for k in keys)
    assert sealed_column(client) == before, "การอ่านและการดูรายการต้องไม่แก้ค่าที่ผนึกไว้"


def test_a_key_issued_while_disabled_stays_unsealed_through_a_rotation(secrets, client):
    secrets()
    bare = issue(client, name="bare")
    secrets(B, previous=A)

    assert sealed_column(client)[bare["id"]] == ""
    response = reveal(client, bare["id"])
    assert response.status_code == 400
    message = response.json()["error"]["message"].lower()
    assert "hash" in message and "before key reveal was switched on" in message

    secrets()            # ไม่เคยเปิดฟีเจอร์: บอกตามนั้น ไม่ใช่ "ออกก่อนเปิด"
    assert "is unset" in reveal(client, bare["id"]).json()["error"]["message"]


def test_mixed_old_rows_all_open_under_current_plus_previous(secrets, client):
    """ฐานจริงระหว่างเปลี่ยนมีครบทุกแบบปนกัน: v1 ใต้ตัวเก่า · ของรุ่นนี้ใต้ตัวเก่า · ของใหม่"""
    old_v1 = issued_under(client, secrets, A, 2, prefix="v1", fmt="v1")
    old_new = issued_under(client, secrets, A, 2, prefix="n", fmt="new")
    secrets(B, previous=A)
    mid = issue(client, name="mid-rotation")

    assert all(opens(client, created) for created in [*old_v1, *old_new, mid])
    as_deployed_version_wrote(client, mid, B)     # v1 ใต้ตัวปัจจุบันก็ต้องยังเป็น current
    assert listed(client)[mid["id"]]["seal_state"] == "current"


# ── log ระดับ DEBUG ─────────────────────────────────────────────────────────

def test_debug_logging_does_not_copy_the_database_into_the_log(secrets, client, caplog):
    """GW_LOG_LEVEL=DEBUG ต้องไม่ทำให้ค่าที่ผนึกและ hash ของ key ไหลลง log

    เจอตอนเขียนเทสการผนึกใหม่ (2026-10-09): aiosqlite เขียนทุกคำสั่ง SQL **พร้อมค่าที่ผูก**
    ที่ระดับ DEBUG · การเก็บสำเนาแลกมาด้วยข้อสัญญาว่า "ฐานที่หลุดไปอย่างเดียวไม่เผยอะไร"
    ซึ่งไม่มีความหมายถ้า log — ที่ถูกส่งต่อและเก็บนานกว่าฐาน — มีของชุดเดียวกันอยู่
    """
    import logging

    from app.core.auth import hash_api_key

    caplog.set_level(logging.DEBUG)
    (created,) = issued_under(client, secrets, A, 1, prefix="x", fmt="new")
    stored = sealed_column(client)[created["id"]]
    listed(client)

    # ยืนยันว่ากำลังจับ log ระดับ DEBUG อยู่จริง ไม่ใช่ผ่านเพราะไม่มีอะไรถูกจับเลย
    logging.getLogger("app.tests.marker").debug("debug-capture-is-live")
    assert "debug-capture-is-live" in caplog.text
    assert stored.rsplit(":", 1)[-1] not in caplog.text, "ค่าที่ผนึก"
    assert hash_api_key(created["api_key"]) not in caplog.text, "hash ที่ใช้ยืนยันตัว"
    assert created["api_key"] not in caplog.text
