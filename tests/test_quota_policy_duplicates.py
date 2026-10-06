"""นโยบายสองใบของเป้าหมายเดียวกัน: ใบไหนชนะต้องแน่นอน และใบที่แพ้ต้องไม่ถูกแสดงว่ามีผล

ตรวจพบ 2026-10-06: นโยบายกลาง day 1 ครั้ง ("old") แล้วสร้างอีกใบ day 100 ครั้ง
("new, raised") → 201 · รายการแสดงทั้งสองใบว่าเปิดอยู่ · คำขอที่สองของสมาชิกยังได้ 429
จากใบเก่า — การขึ้นลิมิตที่หน้าจอบอกว่าสำเร็จแต่ไม่มีผล

และ `resolve_limits` อ่านนโยบายโดยไม่มี ORDER BY: บน SQLite ลำดับที่ได้คือลำดับที่แทรก
ใบเก่าจึงชนะเสมอ แต่บน PostgreSQL แถวที่ถูก UPDATE ย้ายที่ได้ ผู้ชนะจึงสลับได้หลังมีคน
แก้นโยบายใบหนึ่ง
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

CODING = "http://dgx03:8000/v1/chat/completions"
VISION = "http://dgx02:8000/v1/chat/completions"
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


@pytest.fixture(autouse=True)
def upstream():
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        respx.post(VISION).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def create(client, **body):
    return client.post("/admin/quota-policies", headers=auth(client.admin_key), json=body)


def edit(client, policy_id, **body):
    return client.patch(f"/admin/quota-policies/{policy_id}",
                        headers=auth(client.admin_key), json=body)


def listing(client, key=None) -> dict[str, dict]:
    rows = client.get("/admin/quota-policies",
                      headers=auth(key or client.admin_key)).json()["data"]
    return {r["id"]: r for r in rows}


def ask(client, key, model="coding"):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def write_policy(client, *, created_at: datetime, policy_id: str | None = None, **columns):
    """นโยบายที่เขียนลงตารางตรง ๆ — แถวแบบที่ฐานข้อมูลซึ่งใช้งานมาก่อนมีอยู่แล้ว
    (สร้างไว้ตอนที่ API ยังรับใบซ้ำ) พร้อมเวลาสร้างที่กำหนดเอง"""
    from app.db.models import QuotaPolicy
    from app.db.session import session_scope

    async def write():
        async with session_scope() as session:
            row = QuotaPolicy(created_at=created_at, updated_at=created_at, **columns)
            if policy_id:
                row.id = policy_id
            session.add(row)
            await session.flush()
            return row.id

    return client.portal.call(write)


def expire(client, policy_id) -> None:
    from app.db.models import QuotaPolicy
    from app.db.session import session_scope

    async def write():
        async with session_scope() as session:
            row = await session.get(QuotaPolicy, policy_id)
            row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)

    client.portal.call(write)


LAST_WEEK = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
YESTERDAY = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# สร้างซ้ำ → 409
# ---------------------------------------------------------------------------
def test_a_second_policy_for_the_same_target_and_window_is_refused(client, member_key):
    """เคสที่ตรวจพบ: "old" 1 ครั้ง/วัน แล้ว "new, raised" 100 ครั้ง/วัน"""
    old = create(client, scope="global", window="day", max_requests=1, name="old").json()

    new = create(client, scope="global", window="day", max_requests=100, name="new, raised")

    assert new.status_code == 409, new.text
    error = new.json()["error"]
    assert error["code"] == "CONFLICT"
    assert "old" in error["message"], "ต้องบอกว่าชนกับใบไหน"
    assert error["details"]["existing_policy_id"] == old["id"]
    assert list(listing(client)) == [old["id"]], "ใบที่ถูกปฏิเสธต้องไม่ถูกสร้าง"
    # และทางที่ถูกคือแก้ใบเดิม — ซึ่งมีผลจริง
    assert edit(client, old["id"], max_requests=100).status_code == 200
    assert [ask(client, member_key).status_code for _ in range(3)] == [200, 200, 200]


@pytest.mark.parametrize("first, second", [
    ({"scope": "global"}, {"scope": "global", "model_alias": "coding"}),
    ({"scope": "global", "model_alias": "coding"},
     {"scope": "global", "model_alias": "gemma-vision"}),
    ({"scope": "global", "window": "day"}, {"scope": "global", "window": "month"}),
], ids=["with-and-without-model", "two-models", "two-windows"])
def test_policies_that_are_not_the_same_thing_are_not_duplicates(client, first, second):
    assert create(client, max_requests=5, **first).status_code == 201
    assert create(client, max_requests=5, **second).status_code == 201


def test_two_people_can_each_have_their_own(client):
    people = [client.post("/admin/users", headers=auth(client.admin_key),
                          json={"external_id": f"p{i}"}).json() for i in range(2)]
    for person in people:
        assert create(client, scope="user", user_id=person["id"],
                      max_requests=5).status_code == 201
    assert create(client, scope="user", user_id=people[0]["id"],
                  max_requests=9).status_code == 409


def test_a_key_may_have_one_ceiling_per_window(client):
    """เพดานของ key ทุกใบถูกบังคับ — สองใบคนละหน้าต่างมีความหมาย สองใบหน้าต่างเดียวกันควรเป็นใบเดียว"""
    person = client.post("/admin/users", headers=auth(client.admin_key),
                         json={"external_id": "ci"}).json()
    key = client.post("/admin/api-keys", headers=auth(client.admin_key),
                      json={"user_id": person["id"], "name": "ci"}).json()

    assert create(client, scope="key", api_key_id=key["id"], window="day",
                  max_requests=100).status_code == 201
    month = create(client, scope="key", api_key_id=key["id"], window="month",
                   max_requests=1000)
    assert month.status_code == 201
    assert month.json()["effective"] is True and month.json()["shadowed_by"] is None
    assert create(client, scope="key", api_key_id=key["id"], window="day",
                  max_output_tokens=5).status_code == 409


def test_an_expired_policy_does_not_block_its_replacement(client, member_key):
    old = create(client, scope="global", window="day", max_requests=1, expires_in_days=1,
                 name="exam week").json()
    expire(client, old["id"])

    new = create(client, scope="global", window="day", max_requests=2, name="after exams")

    assert new.status_code == 201, new.text
    assert [ask(client, member_key).status_code for _ in range(3)] == [200, 200, 429]
    rows = listing(client)
    assert (rows[old["id"]]["expired"], rows[old["id"]]["effective"]) == (True, False)
    assert rows[new.json()["id"]]["effective"] is True


def test_reviving_an_expired_policy_over_its_replacement_is_refused(client):
    """ต่ออายุใบที่หมดอายุไปแล้วกลับมา = ได้สองใบซ้ำ และใบเก่า (ที่เพิ่งฟื้น) จะบังใบที่ตั้งแทน"""
    old = create(client, scope="global", window="day", max_requests=1, expires_in_days=1,
                 name="exam week").json()
    expire(client, old["id"])
    create(client, scope="global", window="day", max_requests=2, name="after exams")

    revived = edit(client, old["id"], days=7)

    assert revived.status_code == 409
    assert "after exams" in revived.json()["error"]["message"]
    assert listing(client)[old["id"]]["expired"] is True, "ต้องไม่ถูกต่ออายุไปแล้วครึ่งทาง"


def test_moving_a_window_onto_another_policy_is_refused(client):
    create(client, scope="global", window="day", max_requests=1, name="daily")
    monthly = create(client, scope="global", window="month", max_requests=50,
                     name="monthly").json()

    moved = edit(client, monthly["id"], window="day")

    assert moved.status_code == 409
    assert listing(client)[monthly["id"]]["window"] == "month"
    assert edit(client, monthly["id"], window="term").status_code == 200
    assert edit(client, monthly["id"], max_requests=60).status_code == 200, (
        "แก้ลิมิตของใบที่มีอยู่ต้องไม่ถูกนับว่าชนกับตัวเอง")


# ---------------------------------------------------------------------------
# ใบที่ถูกบัง: ต้องถูกแสดงว่าไม่มีผล และบอกว่าใบไหนบัง
# ---------------------------------------------------------------------------
def test_the_same_target_with_another_window_is_created_but_marked_as_unused(
        client, member_key):
    daily = create(client, scope="global", window="day", max_requests=1, name="daily").json()

    monthly = create(client, scope="global", window="month", max_requests=500, name="monthly")

    assert monthly.status_code == 201
    assert monthly.json()["effective"] is False, "คำตอบของการสร้างต้องบอกเลยว่ามันไม่มีผล"
    assert monthly.json()["shadowed_by"] == {"id": daily["id"], "name": "daily"}

    rows = listing(client)
    assert (rows[daily["id"]]["effective"], rows[daily["id"]]["shadowed_by"]) == (True, None)
    assert rows[monthly.json()["id"]]["effective"] is False
    assert rows[monthly.json()["id"]]["shadowed_by"] == {"id": daily["id"], "name": "daily"}
    # และรายการพูดความจริง: ใบที่บังคับใช้คือ daily
    assert [ask(client, member_key).status_code for _ in range(2)] == [200, 429]


def test_twins_that_already_exist_are_resolved_oldest_first_whatever_order_they_sit_in(
        client, member_key):
    """แถวซ้ำที่มีอยู่ก่อนแล้ว · ใบใหม่ถูกเขียนลงตารางก่อนใบเก่า — ลำดับที่ฐานข้อมูลคืนโดย
    ไม่มี ORDER BY จะให้ใบใหม่ชนะ ซึ่งคือสิ่งที่ PostgreSQL ทำได้หลัง UPDATE"""
    newer = write_policy(client, created_at=YESTERDAY, scope="global", window="day",
                         max_requests=100, name="new, raised")
    older = write_policy(client, created_at=LAST_WEEK, scope="global", window="day",
                         max_requests=1, name="old")

    assert [ask(client, member_key).status_code for _ in range(2)] == [200, 429], (
        "ใบที่เก่ากว่าชนะ — เหมือนที่เป็นมาตลอดบน SQLite")

    rows = listing(client)
    assert list(rows) == [older, newer], "รายการเรียงตามลำดับตัดสิน"
    assert rows[older]["effective"] is True
    assert rows[newer]["effective"] is False
    assert rows[newer]["shadowed_by"] == {"id": older, "name": "old"}


def test_equal_creation_times_are_settled_by_id_not_by_chance(client, member_key):
    write_policy(client, created_at=LAST_WEEK, policy_id="b" * 32, scope="global",
                 window="day", max_requests=100)
    write_policy(client, created_at=LAST_WEEK, policy_id="a" * 32, scope="global",
                 window="day", max_requests=1)

    assert [ask(client, member_key).status_code for _ in range(2)] == [200, 429]
    assert listing(client)["b" * 32]["shadowed_by"]["id"] == "a" * 32


def test_the_shadowed_twin_takes_over_when_the_winner_is_deleted(client, member_key):
    older = write_policy(client, created_at=LAST_WEEK, scope="global", window="day",
                         max_requests=1, name="old")
    newer = write_policy(client, created_at=YESTERDAY, scope="global", window="day",
                         max_requests=3, name="new")
    assert listing(client)[newer]["effective"] is False

    client.delete(f"/admin/quota-policies/{older}", headers=auth(client.admin_key))

    row = listing(client)[newer]
    assert (row["effective"], row["shadowed_by"]) == (True, None)
    assert [ask(client, member_key).status_code for _ in range(4)] == [200, 200, 200, 429]


def test_the_people_view_names_the_policy_that_wins(client, member_key):
    """หน้าจอของคนต้องเห็นใบเดียวกับที่ทางเดินของคำขอใช้ — ไม่ใช่ใบที่ถูกบัง"""
    older = write_policy(client, created_at=LAST_WEEK, scope="global", window="day",
                         max_requests=1, name="old")
    write_policy(client, created_at=YESTERDAY, scope="global", window="day",
                 max_requests=100, name="new, raised")
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    me = next(u for u in users if u["external_id"] == "6412345678")

    shown = client.get(f"/admin/users/{me['id']}/quota", headers=auth(client.admin_key)).json()

    assert (shown["policy_id"], shown["limits"]["max_requests"]) == (older, 1)


# ---------------------------------------------------------------------------
# ผู้จัดการเห็นเฉพาะนโยบายของคน/วิชา/key ที่ตัวเองมองเห็น
# ---------------------------------------------------------------------------
@pytest.fixture
def two_classes(client):
    def user(external_id, role="member"):
        return client.post("/admin/users", headers=auth(client.admin_key),
                           json={"external_id": external_id, "role": role}).json()

    def workspace(code):
        return client.post("/admin/workspaces", headers=auth(client.admin_key),
                           json={"code": code, "name": code}).json()

    def join(ws, person):
        client.post(f"/admin/workspaces/{ws['id']}/join", headers=auth(client.admin_key),
                    json={"user_id": person["id"]})

    def key(person, name):
        return client.post("/admin/api-keys", headers=auth(client.admin_key),
                           json={"user_id": person["id"], "name": name}).json()

    lecturer, mine, theirs = user("lecturer", "manager"), user("student-cs"), user("student-art")
    cs101, art200 = workspace("CS101"), workspace("ART200")
    join(cs101, lecturer)
    join(cs101, mine)
    join(art200, theirs)
    return {
        "lecturer_key": key(lecturer, "lecturer")["api_key"],
        "mine": mine, "theirs": theirs, "cs101": cs101, "art200": art200,
        "my_students_key": key(mine, "cs-laptop"), "their_students_key": key(theirs, "art-ci"),
    }


def test_a_manager_does_not_see_ceilings_on_keys_outside_their_classes(client, two_classes):
    """เพดานของ key ไม่มีทั้ง workspace และ user — ตัวกรองเดิมจึงนับมันเป็นนโยบายกลาง
    แล้วแสดงให้ผู้จัดการทุกคน พร้อมชื่อ key ของคนในวิชาอื่น"""
    c = two_classes
    ours = create(client, scope="key", api_key_id=c["my_students_key"]["id"],
                  max_requests=5, name="เพดานของใบ cs-laptop").json()
    foreign = create(client, scope="key", api_key_id=c["their_students_key"]["id"],
                     max_requests=5, name="เพดานของใบ art-ci").json()

    seen = listing(client, c["lecturer_key"])

    assert ours["id"] in seen
    assert foreign["id"] not in seen
    assert set(listing(client)) == {ours["id"], foreign["id"]}, "ผู้ดูแลยังเห็นทั้งหมด"


def test_a_manager_sees_what_binds_their_own_classes_and_people(client, two_classes):
    c = two_classes
    everyone = create(client, scope="global", max_requests=500).json()
    our_class = create(client, scope="workspace", workspace_id=c["cs101"]["id"],
                       max_requests=50).json()
    other_class = create(client, scope="workspace", workspace_id=c["art200"]["id"],
                         max_requests=50).json()
    our_student = create(client, scope="user", user_id=c["mine"]["id"], max_requests=9).json()
    other_student = create(client, scope="user", user_id=c["theirs"]["id"],
                           max_requests=9).json()

    seen = set(listing(client, c["lecturer_key"]))

    assert {everyone["id"], our_class["id"], our_student["id"]} <= seen
    assert not {other_class["id"], other_student["id"]} & seen
