"""ผู้จัดการเอื้อมถึงของนอก workspace ตัวเองไม่ได้ แม้จะรู้ id

`tests/test_manager_scope.py` คุมเส้นทางที่ *แสดงรายการ* — ผู้จัดการเห็นแค่คน key และ
การใช้งานของวิชาตัวเอง · ไฟล์นี้คุมเส้นทางที่ *รับ id ตรง ๆ* ซึ่งเป็นที่ที่การกรองรายการ
ช่วยอะไรไม่ได้: ถ้า endpoint ไม่ถามเองว่า "id นี้เป็นของคุณไหม" การซ่อนไว้ในรายการก็เป็น
แค่การซ่อน

เคสจริง (ตรวจ 2026-10-06 บน a5e9295): อาจารย์ของ CS101 เรียก `/admin/users` เห็นแค่คน
ของตัวเอง แต่ `GET /admin/users/<id ของนักศึกษา ART200>/quota` ตอบ 200 พร้อมโควตาและ
ยอดใช้งานของคนนั้น — เป็นเส้นทางเดียวในกลุ่มที่ถาม `require_manager` แล้วไม่ถามอะไรต่อ

และอีกทางหนึ่งที่การให้สิทธิ์หลุดด่าน: `PATCH /admin/api-keys/{id} {"models": []}`
`[]` บน key แปลว่า "ไม่มีรายการ" = ไม่จำกัด · ด่าน "ห้ามแจกสิ่งที่ตัวเองไม่มี" ตรวจ alias
ทีละตัว จึงไม่มีอะไรให้ตรวจเมื่อรายการว่าง — ผู้จัดการถอดการจำกัดที่ผู้ดูแลตั้งไว้ได้
"""

from __future__ import annotations

import httpx
import pytest
import respx

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


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


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


def key_for(client, person, **extra):
    r = admin(client, "POST", "/admin/api-keys",
              json={"user_id": person["id"], "name": "k", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def models(client, key) -> set[str]:
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def key_row(client, key_id):
    return next(k for k in admin(client, "GET", "/admin/api-keys").json()["data"]
                if k["id"] == key_id)


@pytest.fixture
def two_classes(client):
    """อาจารย์ของ CS101 · นักศึกษาของตัวเองหนึ่งคน · นักศึกษาของ ART200 หนึ่งคน"""
    lecturer = user(client, "lecturer", role="manager")
    cs101 = workspace(client, "CS101", ["coding"])
    art200 = workspace(client, "ART200", ["gemma-vision"])
    mine = user(client, "student-cs")
    theirs = user(client, "student-art")
    join(client, cs101, lecturer)
    join(client, cs101, mine)
    join(client, art200, theirs)
    return {"key": key_for(client, lecturer)["api_key"], "lecturer": lecturer,
            "cs101": cs101, "art200": art200, "mine": mine, "theirs": theirs}


# ── /admin/users/{id}/quota ─────────────────────────────────────────────────

def test_a_manager_cannot_read_the_quota_of_someone_outside_their_workspaces(client, two_classes):
    with respx.mock:
        respx.post(CODING).mock(return_value=httpx.Response(200, json=REPLY))
        # คนนอกมีการใช้งานจริง — สิ่งที่รั่วออกไปคือตัวเลขนี้
        loner = user(client, "no-class-at-all")
        spent = client.post("/v1/chat/completions",
                            headers=auth(key_for(client, loner)["api_key"]),
                            json={"model": "coding",
                                  "messages": [{"role": "user", "content": "hi"}]})
        assert spent.status_code == 200

    for outsider in (two_classes["theirs"], loner):
        peek = client.get(f"/admin/users/{outsider['id']}/quota",
                          headers=auth(two_classes["key"]))
        assert peek.status_code == 400, peek.text
        body = peek.json()
        assert body["error"]["message"] == "User not found."
        assert outsider["external_id"] not in peek.text
        assert "used" not in body


def test_not_yours_and_not_there_are_the_same_answer(client, two_classes):
    """ไม่งั้น endpoint นี้กลายเป็นเครื่องถามว่า id ไหนมีอยู่จริง"""
    outside = client.get(f"/admin/users/{two_classes['theirs']['id']}/quota",
                         headers=auth(two_classes["key"]))
    missing = client.get(f"/admin/users/{'0' * 32}/quota", headers=auth(two_classes["key"]))
    assert outside.status_code == missing.status_code == 400
    assert outside.json()["error"]["message"] == missing.json()["error"]["message"]
    assert outside.json()["error"]["code"] == missing.json()["error"]["code"]


def test_a_manager_still_reads_the_quota_of_their_own_people(client, two_classes):
    """ฟอร์มออก key ของคอนโซลเรียกเส้นทางนี้ตอนเลือกคน — ต้องยังใช้ได้กับคนของตัวเอง"""
    for person in (two_classes["mine"], two_classes["lecturer"]):
        mine = client.get(f"/admin/users/{person['id']}/quota", headers=auth(two_classes["key"]))
        assert mine.status_code == 200, mine.text
        assert mine.json()["external_id"] == person["external_id"]


def test_an_admin_reads_anyones_quota(client, two_classes):
    seen = admin(client, "GET", f"/admin/users/{two_classes['theirs']['id']}/quota")
    assert seen.status_code == 200
    assert seen.json()["external_id"] == "student-art"


def test_a_manager_in_no_workspace_reads_only_their_own(client):
    lonely = user(client, "new-manager", role="manager")
    key = key_for(client, lonely)["api_key"]
    someone = user(client, "anyone")

    assert client.get(f"/admin/users/{lonely['id']}/quota", headers=auth(key)).status_code == 200
    assert client.get(f"/admin/users/{someone['id']}/quota", headers=auth(key)).status_code == 400


# ── PATCH /admin/api-keys/{id} {"models": []} ────────────────────────────────

def test_a_manager_cannot_lift_a_list_to_reach_another_classs_models(client, two_classes):
    """นักศึกษาอยู่สองวิชา · ผู้ดูแลจำกัด key ของเขาไว้ที่ coding · อาจารย์ CS101 ถอดออกไม่ได้

    ถอดแล้ว key ใบนั้นจะเรียก gemma-vision ของ ART200 ได้ ซึ่งอาจารย์คนนี้ทั้งใช้เองไม่ได้
    และแจกไม่ได้ด้วยทางอื่นทุกทาง
    """
    both = user(client, "in-both")
    join(client, two_classes["cs101"], both)
    join(client, two_classes["art200"], both)
    narrowed = key_for(client, both, models=["coding"])
    assert models(client, narrowed["api_key"]) == {"coding"}

    lifted = client.patch(f"/admin/api-keys/{narrowed['id']}",
                          headers=auth(two_classes["key"]), json={"models": []})

    assert lifted.status_code == 403, lifted.text
    assert lifted.json()["error"]["details"]["models"] == ["gemma-vision"]
    assert key_row(client, narrowed["id"])["models"] == ["coding"]
    assert models(client, narrowed["api_key"]) == {"coding"}


def test_a_manager_cannot_lift_the_list_on_their_own_narrowed_key_with_that_key(client):
    """key ที่ออกให้สคริปต์เดียว ต้องไม่ปลดการจำกัดของตัวเองได้

    ผู้จัดการเรียก admin API ด้วย key ได้ (ตั้งใจ) · ใบที่ถูกจำกัดไว้ที่ coding จึงเป็น
    "ผู้กระทำ" ที่ใช้ได้แค่ coding — แต่ `{"models": []}` ไม่มี alias ให้ด่านตรวจ
    """
    boss = user(client, "lecturer", role="manager")
    join(client, workspace(client, "CS101", ["coding", "gemma-vision"]), boss)
    script = key_for(client, boss, models=["coding"])
    assert models(client, script["api_key"]) == {"coding"}

    lifted = client.patch(f"/admin/api-keys/{script['id']}",
                          headers=auth(script["api_key"]), json={"models": []})

    assert lifted.status_code == 403, lifted.text
    assert models(client, script["api_key"]) == {"coding"}


def test_a_manager_may_lift_a_list_when_nothing_beyond_their_reach_opens_up(client, two_classes):
    """ด่านนี้ต้องไม่กลายเป็น "ผู้จัดการถอดรายการไม่ได้เลย" — คนที่อยู่แค่วิชาของเขา ถอดได้"""
    narrowed = key_for(client, two_classes["mine"], models=["coding"])

    lifted = client.patch(f"/admin/api-keys/{narrowed['id']}",
                          headers=auth(two_classes["key"]), json={"models": []})

    assert lifted.status_code == 200, lifted.text
    assert key_row(client, narrowed["id"])["models"] == []
    assert models(client, narrowed["api_key"]) == {"coding"}


def test_sending_an_empty_list_to_a_key_that_has_none_is_not_a_grant(client, two_classes):
    """ไม่มีอะไรถูกถอด จึงไม่มีอะไรให้ปฏิเสธ — เช่นคอนโซลบันทึกฟอร์มเดิมซ้ำพร้อมเปลี่ยนวันหมดอายุ"""
    both = user(client, "in-both")
    join(client, two_classes["cs101"], both)
    join(client, two_classes["art200"], both)
    open_key = key_for(client, both)

    same = client.patch(f"/admin/api-keys/{open_key['id']}",
                        headers=auth(two_classes["key"]), json={"models": [], "days": 30})
    assert same.status_code == 200, same.text


def test_an_admin_lifts_any_list(client, two_classes):
    both = user(client, "in-both")
    join(client, two_classes["cs101"], both)
    join(client, two_classes["art200"], both)
    narrowed = key_for(client, both, models=["coding"])

    assert admin(client, "PATCH", f"/admin/api-keys/{narrowed['id']}",
                 json={"models": []}).status_code == 200
    assert models(client, narrowed["api_key"]) == {"coding", "gemma-vision"}


# ── กวาดทุกเส้นทางของผู้จัดการที่รับ id ───────────────────────────────────────
#
# เส้นทางเดียวที่หลุดคือ /quota ข้างบน แต่มันหลุดได้เพราะไม่มีอะไรไล่ถามทุกเส้นทางด้วย
# คำถามเดียวกัน · ตารางนี้คือคำถามนั้น: ส่ง id ของวิชาอื่น / คนของวิชาอื่น / key ของคน
# วิชาอื่น เข้าไปทุกช่องที่รับ id แล้วต้องถูกปฏิเสธ และ **ต้องไม่มีอะไรเปลี่ยน**

@pytest.fixture
def outside(client, two_classes):
    """ของของ ART200 ที่อาจารย์ CS101 ไม่ควรแตะได้ พร้อมคำขอจริงหนึ่งรายการ"""
    theirs = two_classes["theirs"]
    key = key_for(client, theirs)
    with respx.mock:
        respx.post(url__regex=r"http://dgx0\d:8000/.*").mock(
            return_value=httpx.Response(200, json=REPLY))
        spent = client.post("/v1/chat/completions", headers=auth(key["api_key"]),
                            json={"model": "gemma-vision",
                                  "messages": [{"role": "user", "content": "hi"}]})
    assert spent.status_code == 200, spent.text
    return {**two_classes, "their_key": key,
            "their_request": spent.headers["x-litegate-request-id"]}


def _snapshot(client):
    """ทุกอย่างที่เส้นทางในตารางเปลี่ยนได้ — เทียบก่อน/หลังเพื่อยืนยันว่าไม่มีอะไรขยับ"""
    spaces = admin(client, "GET", "/admin/workspaces").json()["data"]
    keys = admin(client, "GET", "/admin/api-keys").json()["data"]
    return (
        sorted((w["code"], w["status"], tuple(w["models"]),
                tuple(sorted(m["id"] for m in w["members"]))) for w in spaces),
        sorted((k["id"], k["revoked"], tuple(k["models"]), k["expires_at"]) for k in keys),
    )


REFUSED = (400, 403, 404)

WRITES = [
    ("POST", "/admin/workspaces/{art}/members", {"user_ids": ["{mine}"]}),
    ("POST", "/admin/workspaces/{art}/join", {"user_id": "{mine}"}),
    ("DELETE", "/admin/workspaces/{art}/members/{theirs}", None),
    ("PATCH", "/admin/workspaces/{art}/status", {"status": "suspended"}),
    ("POST", "/admin/workspaces/{art}/models", {"models": []}),
    ("POST", "/admin/api-keys", {"user_id": "{theirs}"}),
    ("POST", "/admin/api-keys", {"user_id": "{mine}", "workspace_id": "{art}"}),
    ("PATCH", "/admin/api-keys/{their_key}", {"days": 3}),
    ("PATCH", "/admin/api-keys/{their_key}", {"models": ["coding"]}),
    ("DELETE", "/admin/api-keys/{their_key}", None),
    # เส้นทางของ admin ล้วน — ผู้จัดการต้องไม่ผ่านด่านแรกด้วยซ้ำ
    ("PATCH", "/admin/users/{theirs}", {"role": "manager"}),
    ("PATCH", "/admin/users/{mine}", {"status": "suspended"}),
    ("POST", "/admin/users/{theirs}/quota/reset", None),
    ("POST", "/admin/api-keys/{their_key}/reveal", None),
    ("DELETE", "/admin/api-keys/{their_key}/purge", None),
    ("DELETE", "/admin/workspaces/{art}", None),
    ("DELETE", "/admin/workspaces/{cs}", None),
]

READS = [
    "/admin/users/{theirs}/quota",
    "/admin/usage/summary?workspace_id={art}",
    "/admin/usage/savings?workspace_id={art}",
    "/admin/models/gemma-vision/compatibility",
    "/admin/api-keys/{their_key}/reveals",
]


def _fill(template, ids):
    if isinstance(template, str):
        return template.format(**ids)
    if isinstance(template, list):
        return [_fill(item, ids) for item in template]
    if isinstance(template, dict):
        return {k: _fill(v, ids) for k, v in template.items()}
    return template


def _ids(outside):
    return {"art": outside["art200"]["id"], "cs": outside["cs101"]["id"],
            "mine": outside["mine"]["id"], "theirs": outside["theirs"]["id"],
            "their_key": outside["their_key"]["id"]}


@pytest.mark.parametrize("method, path, body", WRITES,
                         ids=[f"{m} {p}" for m, p, _ in WRITES])
def test_no_write_reaches_outside_the_managers_workspaces(client, outside, method, path, body):
    ids = _ids(outside)
    before = _snapshot(client)

    response = client.request(method, _fill(path, ids), headers=auth(outside["key"]),
                              **({"json": _fill(body, ids)} if body is not None else {}))

    assert response.status_code in REFUSED, f"{response.status_code} {response.text[:300]}"
    assert _snapshot(client) == before
    # key ของคนนอกยังใช้ได้เหมือนเดิมทุกประการ
    assert models(client, outside["their_key"]["api_key"]) == {"gemma-vision"}


@pytest.mark.parametrize("path", READS)
def test_no_read_reaches_outside_the_managers_workspaces(client, outside, path):
    response = client.get(_fill(path, _ids(outside)), headers=auth(outside["key"]))
    assert response.status_code in REFUSED, f"{response.status_code} {response.text[:300]}"
    assert "student-art" not in response.text


def test_listings_filtered_by_an_outside_id_come_back_empty(client, outside):
    """เส้นทางที่รับ id เป็นตัวกรอง ตอบ 200 ได้ — แต่ต้องไม่มีแถวของคนนอกอยู่ในนั้น"""
    ids = _ids(outside)
    keys = client.get(f"/admin/api-keys?user_id={ids['theirs']}", headers=auth(outside["key"]))
    assert keys.status_code == 200 and keys.json()["data"] == []

    found = client.get(f"/admin/usage/requests?id={outside['their_request']}",
                       headers=auth(outside["key"]))
    assert found.status_code == 200 and found.json()["data"] == []
    # ผู้ดูแลหาแถวเดียวกันเจอ — ยืนยันว่าที่ว่างข้างบนคือเพราะถูกกรอง ไม่ใช่เพราะไม่มีแถว
    seen = admin(client, "GET", f"/admin/usage/requests?id={outside['their_request']}")
    assert [r["user_id"] for r in seen.json()["data"]] == [ids["theirs"]]

    for report in ("/admin/usage/top-users", "/admin/usage/by-key", "/admin/usage/quota"):
        body = client.get(report, headers=auth(outside["key"]))
        assert body.status_code == 200
        assert ids["theirs"] not in body.text and ids["their_key"] not in body.text, report
