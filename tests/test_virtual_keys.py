"""A key that carries its own model list.

Until now a key's reach came only from the workspace it was bound to, so
issuing a key for one specific model meant creating a workspace for it. LiteLLM
puts the list on the key, which is the shape people expect, and it is the
smaller change: absent list means exactly today's behaviour.

The property that has to hold is that the list only ever narrows. A key naming
a model its owner's workspace forbids must still be refused — otherwise issuing
a key becomes a way around workspace policy, and whoever set that policy would
never see it happen.
"""

from __future__ import annotations


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _issue(client, models=None, workspace_id=None, user_id=None, expect=201):
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    body = {"user_id": user_id or users[0]["id"], "name": "test"}
    if models is not None:
        body["models"] = models
    if workspace_id:
        body["workspace_id"] = workspace_id
    r = client.post("/admin/api-keys", headers=auth(client.admin_key), json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _call(client, key, alias):
    return client.post(
        "/v1/chat/completions",
        headers=auth(key),
        json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
    )


# ── การออก key ──────────────────────────────────────────────────────────────

def test_a_key_without_a_list_behaves_as_before(client):
    key = _issue(client)
    assert key["models"] == []


def test_a_key_can_name_the_models_it_may_use(client):
    key = _issue(client, models=["coding"])
    assert key["models"] == ["coding"]
    listed = client.get("/admin/api-keys", headers=auth(client.admin_key)).json()["data"]
    assert next(k["models"] for k in listed if k["id"] == key["id"]) == ["coding"]


def test_an_alias_that_does_not_exist_is_refused_at_issue_time(client):
    """key ที่ระบุ alias ผิดคือ key ที่เรียกอะไรไม่ได้ และไม่มีอะไรบอกจนกว่าจะลอง"""
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    r = client.post(
        "/admin/api-keys",
        headers=auth(client.admin_key),
        json={"user_id": users[0]["id"], "models": ["not-a-model"]},
    )
    assert r.status_code in (400, 404)
    assert "not-a-model" in r.text


# ── การบังคับใช้ ────────────────────────────────────────────────────────────

def test_a_model_on_the_list_is_allowed(client):
    key = _issue(client, models=["coding"])
    # 502/503 = ไปถึง backend แล้ว (ไม่มี backend จริงในเทส) · สิ่งที่วัดคือ "ไม่ใช่ 403"
    assert _call(client, key["api_key"], "coding").status_code != 403


def test_a_model_off_the_list_is_refused(client):
    key = _issue(client, models=["coding"])
    response = _call(client, key["api_key"], "gemma-vision")
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "MODEL_NOT_PERMITTED"
    assert "coding" in body["message"]


def test_the_limit_holds_for_an_admin_key_too(client):
    """ข้อจำกัดที่ผู้ออกตั้งใจใส่ ต้องไม่หายไปเพราะเจ้าของเป็น admin

    ต่างจากกติกาของ workspace ที่ manager/admin ข้ามได้ — อันนั้นคือ "ผู้ดูแลเห็น
    ทุกอย่าง" ส่วนอันนี้คือ key ที่ออกให้สคริปต์ตัวเดียวโดยเจตนา
    """
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    admin = next(u for u in users if u["role"] == "admin")
    key = _issue(client, models=["coding"], user_id=admin["id"])
    assert _call(client, key["api_key"], "gemma-vision").status_code == 403
    assert _call(client, key["api_key"], "coding").status_code != 403


def test_the_list_narrows_and_never_widens(client):
    """key ระบุโมเดลที่ workspace ของเจ้าของไม่อนุญาต — ต้องยังถูกปฏิเสธ

    ไม่งั้นการออก key จะกลายเป็นทางลัดข้ามนโยบายของ workspace โดยที่คนตั้งนโยบาย
    ไม่มีทางรู้
    """
    member = client.post("/admin/users", headers=auth(client.admin_key),
                         json={"external_id": "6412999", "role": "member"}).json()
    ws = client.post("/admin/workspaces", headers=auth(client.admin_key),
                     json={"code": "CS101", "name": "class"}).json()
    # workspace อนุญาตแค่ coding
    client.post(f"/admin/workspaces/{ws['id']}/models", headers=auth(client.admin_key),
                json={"models": ["coding"]})
    # แต่ key ขอ gemma-vision ด้วย
    key = _issue(client, models=["coding", "gemma-vision"],
                 workspace_id=ws["id"], user_id=member["id"])

    assert _call(client, key["api_key"], "coding").status_code != 403
    assert _call(client, key["api_key"], "gemma-vision").status_code == 403


def test_an_empty_list_is_not_a_deny_all(client):
    """[] คือ "ไม่ระบุ" — ไม่ใช่ "ห้ามทุกอย่าง" ต่างจากกติกาของ workspace โดยตั้งใจ"""
    key = _issue(client, models=[])
    assert _call(client, key["api_key"], "coding").status_code != 403
