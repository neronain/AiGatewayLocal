"""มัดบน key ที่ไม่ให้อะไรเลย ต้องแปลว่า "เรียกอะไรไม่ได้" ไม่ใช่ "ไม่มีอะไรจำกัด"

เคสจริง (ตรวจ 2026-10-09 บน c1876db): key ของสมาชิกจำกัดไว้ที่มัด `coding-set`
(= `coding`) · ผู้ดูแลกด *ปิด* มัดนั้น — ซึ่งทั้งคอนโซลและคำปฏิเสธของ DELETE บอกว่าเป็น
วิธี "หยุดไม่ให้มันให้สิทธิ์อะไร โดยไม่เสียรายการ" —

    ก่อนปิด   GET /v1/models → coding
    หลังปิด   GET /v1/models → coding, gemma-vision, muse-local

การปิดมัดจึง *เพิ่ม* สิทธิ์ให้ทุกใบที่ถูกจำกัดด้วยมัดนั้น: `_group_models` ขยายมัดที่ปิด
เป็นเซตว่างตามที่ตั้งใจ แต่ `permitted_aliases` ถามว่า "ขยายแล้วได้อะไรไหม" แทนที่จะ
ถามว่า "ใบนี้เขียนข้อจำกัดไว้ไหม" — เซตว่างจึงถูกอ่านว่าไม่ได้เขียนอะไรไว้ · ลบมัดทิ้ง
(ทำได้ เพราะด่านลบนับแต่ workspace กับนโยบายโควตา) ให้ผลเดียวกันและกู้คืนไม่ได้

บั๊กรูปเดียวกับที่ `_models_via_membership` เขียนกันไว้แล้วฝั่ง workspace: "ระงับวิชา
แล้วนักเรียนได้ทั้งแค็ตตาล็อก"
"""

from __future__ import annotations

import httpx
import pytest
import respx

ALL = {"coding", "gemma-vision", "muse-local"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def person(client, external_id="s1", role="member"):
    return admin(client, "POST", "/admin/users",
                 json={"external_id": external_id, "role": role}).json()


def bundle(client, name="coding-set", models=("coding",)):
    made = admin(client, "POST", "/admin/access-groups",
                 json={"name": name, "models": list(models)})
    assert made.status_code == 201, made.text
    return made.json()


def key_for(client, who, **extra) -> dict:
    made = admin(client, "POST", "/admin/api-keys",
                 json={"user_id": who["id"], "name": "k", **extra})
    assert made.status_code == 201, made.text
    return made.json()


def catalogue(client, key: str) -> set[str]:
    return {m["id"] for m in client.get("/v1/models", headers=auth(key)).json()["data"]}


def switch(client, group, on: bool):
    response = admin(client, "PATCH", f"/admin/access-groups/{group['id']}",
                     json={"enabled": on})
    assert response.status_code == 200, response.text


def ask(client, key: str, model: str):
    """คำขอจริงผ่านด่านจริง · backend ถูกจำลองไว้ — ถ้าด่านปล่อยผ่าน (ซึ่งคือบั๊กที่ไฟล์นี้จับ)
    คำขอต้องไม่วิ่งออกไปหาเครื่องที่ config/ ตั้งชื่อไว้"""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"http://dgx0\d:8000/.*").mock(
            return_value=httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }))
        return client.post(
            "/v1/chat/completions", headers=auth(key),
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


# ── ตัว defect ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["member", "manager", "admin"])
def test_switching_a_bundle_off_takes_its_models_away_from_the_key(client, role):
    """ปิดมัด = ใบที่ถูกจำกัดด้วยมัดนั้นเรียกอะไรไม่ได้ · ไม่ใช่ได้ทั้งแค็ตตาล็อก"""
    group = bundle(client)
    key = key_for(client, person(client, role=role), access_groups=[group["id"]])["api_key"]
    assert catalogue(client, key) == {"coding"}

    switch(client, group, on=False)

    assert catalogue(client, key) == set()
    for model in sorted(ALL):
        assert ask(client, key, model).status_code == 403, model


def test_switching_it_back_on_is_all_it_takes(client):
    """ปิดมัดคือทางที่ "ย้อนกลับได้" ตามที่คอนโซลบอก — ต้องย้อนได้จริง"""
    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])["api_key"]

    switch(client, group, on=False)
    switch(client, group, on=True)

    assert catalogue(client, key) == {"coding"}


def test_the_refusal_names_the_bundle_rather_than_a_list_nobody_wrote(client):
    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])["api_key"]
    switch(client, group, on=False)

    error = ask(client, key, "coding").json()["error"]

    assert error["code"] == "MODEL_NOT_PERMITTED"
    assert "access group" in error["message"]
    assert "switched off" in error["message"]
    assert error["details"]["allowed"] == []


def test_a_list_and_a_dead_bundle_leave_the_list(client):
    """ข้อนี้ถูกอยู่แล้วก่อนแก้ — รายการบนใบยังมีของ จึงไม่เคยตกไปเป็น "ไม่จำกัด\""""
    group = bundle(client, models=("gemma-vision",))
    key = key_for(client, person(client), models=["coding"],
                  access_groups=[group["id"]])["api_key"]
    assert catalogue(client, key) == {"coding", "gemma-vision"}

    switch(client, group, on=False)

    assert catalogue(client, key) == {"coding"}


def test_one_live_bundle_among_dead_ones_still_counts(client):
    dead, live = bundle(client, "dead", ("coding",)), bundle(client, "live", ("muse-local",))
    key = key_for(client, person(client), access_groups=[dead["id"], live["id"]])["api_key"]

    switch(client, dead, on=False)

    assert catalogue(client, key) == {"muse-local"}


# ── ลบมัด ─────────────────────────────────────────────────────────────────────

def test_a_bundle_a_live_key_is_limited_by_is_not_deleted(client):
    """ลบแล้วใบนั้นจะไม่มีทางกลับมาใช้ได้ — ปฏิเสธเหมือนที่ปฏิเสธเมื่อ workspace ยังถืออยู่"""
    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])

    response = admin(client, "DELETE", f"/admin/access-groups/{group['id']}")

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert "1 API key(s)" in error["message"] and "disable" in error["message"].lower()
    assert error["details"]["api_keys"] == 1
    assert catalogue(client, key["api_key"]) == {"coding"}


def test_once_the_key_is_revoked_the_bundle_can_go(client):
    """คำปฏิเสธต้องเป็นทางที่เดินต่อได้ ไม่ใช่ทางตัน"""
    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])
    assert admin(client, "DELETE", f"/admin/api-keys/{key['id']}").status_code == 200

    assert admin(client, "DELETE", f"/admin/access-groups/{group['id']}").status_code == 200


def test_a_key_naming_a_bundle_that_is_already_gone_calls_nothing(client):
    """ฐานข้อมูลที่ลบมัดไปแล้วก่อนอัปเกรด: ใบที่ยังชี้ไปหามันต้องไม่ใช่ใบที่ไม่มีอะไรจำกัด"""
    from app.db.models import AccessGroup
    from app.db.session import session_scope

    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])["api_key"]

    async def drop():
        async with session_scope() as session:
            await session.delete(await session.get(AccessGroup, group["id"]))

    client.portal.call(drop)

    assert catalogue(client, key) == set()


def test_the_refusal_tells_the_admin_something_that_can_be_done(client):
    """ "Take it away from them first" ใช้กับ key ไม่ได้ — ไม่มีเส้นทางไหนถอดมัดออกจากใบ

    `PATCH /admin/api-keys/{id}` รับแค่ `days` กับ `models` (ผู้ตรวจอิสระ 2026-10-09) ·
    คำปฏิเสธที่สั่งให้ทำสิ่งที่ทำไม่ได้คือทางตัน: ต้องบอกว่าใบไหน และทางที่มีจริงคือ
    เพิกถอน/ออกใหม่ หรือปิดมัดแทนการลบ
    """
    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])

    error = admin(client, "DELETE", f"/admin/access-groups/{group['id']}").json()["error"]

    message = error["message"]
    assert "Take it away from them first" not in message
    assert key["key_prefix"] in message, "ต้องบอกว่าใบไหน ไม่ใช่แค่กี่ใบ"
    assert "revoke" in message.lower() and "cannot be taken off a key" in message
    assert "disable" in message.lower()
    assert error["details"]["keys"] == [
        {"id": key["id"], "name": "k", "key_prefix": key["key_prefix"]}]


def test_what_a_workspace_holds_is_still_told_apart_from_what_a_key_holds(client):
    """ของ workspace ถอดออกได้จริง — คำแนะนำนั้นต้องยังอยู่ และไม่ปนกับของ key"""
    group = bundle(client)
    ws = admin(client, "POST", "/admin/workspaces", json={"code": "CS101", "name": "CS101"}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/models",
          json={"models": [], "access_groups": [group["id"]]})

    error = admin(client, "DELETE", f"/admin/access-groups/{group['id']}").json()["error"]

    assert "1 workspace(s)" in error["message"]
    assert "Take it away from them first" in error["message"]
    assert "cannot be taken off a key" not in error["message"]


def test_an_expired_key_still_holds_the_bundle_and_the_refusal_says_so(client):
    """ตัดสินไว้: ใบที่หมดอายุยังนับ — หมดอายุกู้ได้ด้วยปุ่ม Extend เพิกถอนกู้ไม่ได้

    ถ้าปล่อยให้ลบมัดได้ ใบที่ต่ออายุกลับมาทีหลังจะเป็นใบที่ชี้ไปหามัดที่ไม่มีอยู่ เรียกอะไร
    ไม่ได้ และไม่มีปุ่มไหนแก้ · แต่ต้องบอกว่าที่ค้างอยู่คือใบหมดอายุ ไม่งั้นผู้ดูแลไล่หาใบ
    ที่ "ยังใช้อยู่" ซึ่งไม่มี
    """
    from datetime import timedelta

    from app.db.models import ApiKey, utcnow
    from app.db.session import session_scope

    group = bundle(client)
    key = key_for(client, person(client), access_groups=[group["id"]])

    async def lapse():
        async with session_scope() as session:
            (await session.get(ApiKey, key["id"])).expires_at = utcnow() - timedelta(days=1)

    client.portal.call(lapse)

    refused = admin(client, "DELETE", f"/admin/access-groups/{group['id']}")
    assert refused.status_code == 400, refused.text
    assert "1 of them expired" in refused.json()["error"]["message"]
    assert refused.json()["error"]["details"]["api_keys_expired"] == 1

    # ต่ออายุกลับมาแล้วใบยังใช้ได้ตามเดิม — นี่คือสิ่งที่การปฏิเสธรักษาไว้
    assert admin(client, "PATCH", f"/admin/api-keys/{key['id']}",
                 json={"days": 30}).status_code == 200
    assert catalogue(client, key["api_key"]) == {"coding"}
