"""Which models a workspace may call, and showing that honestly.

The rule is strict: a key bound to a workspace can only call aliases that
workspace enables, and an absent row means not permitted. So the console
showing an empty set of checkboxes for a workspace that has models enabled is
not a cosmetic problem — pressing Save on that screen wipes the list and every
key bound to it stops working.

The listing never carried the current selection, so the boxes rendered
unchecked no matter what was configured.
"""

from __future__ import annotations


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _workspace(client, code="CS101"):
    r = client.post("/admin/workspaces", headers=auth(client.admin_key),
                    json={"code": code, "name": f"{code} class"})
    assert r.status_code == 201, r.text
    return r.json()


def _listing(client, workspace_id):
    data = client.get("/admin/workspaces", headers=auth(client.admin_key)).json()["data"]
    return next(w for w in data if w["id"] == workspace_id)


def test_a_new_workspace_reports_no_models(client):
    ws = _workspace(client)
    assert _listing(client, ws["id"])["models"] == []


def test_the_listing_carries_what_was_enabled(client):
    """หน้าเว็บติ๊ก checkbox จากค่านี้ — ไม่มีค่านี้ ช่องติ๊กจะว่างทุกครั้ง"""
    ws = _workspace(client)
    client.post(f"/admin/workspaces/{ws['id']}/models",
                headers=auth(client.admin_key), json={"models": ["coding"]})
    assert _listing(client, ws["id"])["models"] == ["coding"]


def test_setting_models_replaces_rather_than_adds(client):
    ws = _workspace(client)
    client.post(f"/admin/workspaces/{ws['id']}/models",
                headers=auth(client.admin_key), json={"models": ["coding"]})
    client.post(f"/admin/workspaces/{ws['id']}/models",
                headers=auth(client.admin_key), json={"models": ["gemma-vision"]})
    assert _listing(client, ws["id"])["models"] == ["gemma-vision"]


def test_an_empty_list_really_does_permit_nothing(client):
    """ต่างจาก "ยังไม่ได้ตั้ง" — key ที่ผูกกับมันจะเรียกอะไรไม่ได้เลย"""
    ws = _workspace(client)
    client.post(f"/admin/workspaces/{ws['id']}/models",
                headers=auth(client.admin_key), json={"models": ["coding"]})
    client.post(f"/admin/workspaces/{ws['id']}/models",
                headers=auth(client.admin_key), json={"models": []})
    assert _listing(client, ws["id"])["models"] == []


def test_an_alias_that_does_not_exist_is_refused(client):
    ws = _workspace(client)
    response = client.post(f"/admin/workspaces/{ws['id']}/models",
                           headers=auth(client.admin_key), json={"models": ["not-a-model"]})
    assert response.status_code in (400, 404)
    assert _listing(client, ws["id"])["models"] == []


def test_each_workspace_keeps_its_own_list(client):
    a = _workspace(client, "CS101")
    b = _workspace(client, "MA201")
    client.post(f"/admin/workspaces/{a['id']}/models",
                headers=auth(client.admin_key), json={"models": ["coding"]})
    client.post(f"/admin/workspaces/{b['id']}/models",
                headers=auth(client.admin_key), json={"models": ["gemma-vision"]})
    assert _listing(client, a["id"])["models"] == ["coding"]
    assert _listing(client, b["id"])["models"] == ["gemma-vision"]
