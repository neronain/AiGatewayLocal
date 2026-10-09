"""key ที่ถูกจำกัดไว้ ต้องไม่พกอำนาจผู้ดูแลของเจ้าของติดไปด้วย

เคสจริง (ตรวจ 2026-10-09 บน c1876db): ผู้ดูแลออก key ให้สคริปต์ตัวหนึ่ง จำกัดไว้ที่
`coding` ตัวเดียว · key ใบนั้น —

    GET   /admin/users                 200   (อ่านรายชื่อทุกคน)
    PATCH /admin/api-keys/<ตัวมันเอง>   200   {"models": []}  ← ถอดรายการของตัวเอง
    GET   /v1/models                   coding, gemma-vision, muse-local

เพราะ `Principal.is_admin` ดูแค่ role ของ *เจ้าของ* · ใบที่ตั้งใจให้ทำได้อย่างเดียว
จึงออก key ใหม่ แก้ registry ลบโควตา และเปิดดู key ของคนอื่นได้ครบ — ข้อจำกัดบนใบ
เป็นแค่คำขอร้อง · หลุดใบเดียวเท่ากับหลุดทั้งเกตเวย์

กติกา (ดู `Principal.key_limits` ใน app/core/auth.py): key ที่มีข้อจำกัดของตัวเองแม้
ข้อเดียว — รายการโมเดล · มัดโมเดล · ผูก workspace · เพดานเฉพาะใบ — ไม่มีอำนาจ admin
หรือ manager ทุกที่ที่อำนาจนั้นถูกอ่าน ไม่ใช่เฉพาะบางเส้นทาง · key ของผู้ดูแลที่ **ไม่มี**
ข้อจำกัดทำงานเหมือนเดิมทุกอย่าง (สคริปต์ดูแลระบบใช้ใบแบบนี้อยู่) และ session ของ
คอนโซลไม่เคยถูกนับว่าจำกัด

หลักเดียวกับ `_require_console` ของ /v1/me/api-keys ("key ที่รั่วต้องออกใบใหม่ให้
ตัวเองไม่ได้") ซึ่งฝั่ง /admin ไม่เคยใช้
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.passwords import hash_password
from app.db.models import ApiKey, AuditLog, QuotaPolicy, User, utcnow
from app.db.session import session_scope

PASSWORD = "correct-horse-battery"
ALL = {"coding", "gemma-vision", "muse-local"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    """บางข้อข้างล่างสั่งแก้ registry จริง — ต้องไม่ใช่ config/ ของ repo"""
    return writable_config


def admin(client, method, path, **kw):
    """ผู้ดูแลตัวจริงของเทส — ใบที่ไม่มีข้อจำกัด"""
    client.cookies.clear()
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def owner_of(client, key: str) -> str:
    client.cookies.clear()
    return client.get("/v1/me", headers=auth(key)).json()["user_id"]


def person(client, external_id: str, role: str) -> dict:
    """บัญชีที่มีรหัสผ่าน — เข้าคอนโซลได้ และผู้ดูแลออก key ให้ได้"""
    made = admin(client, "POST", "/admin/users",
                 json={"external_id": external_id, "role": role})
    assert made.status_code == 201, made.text

    async def set_password():
        async with session_scope() as session:
            (await session.get(User, made.json()["id"])).password_hash = hash_password(PASSWORD)

    client.portal.call(set_password)
    return made.json()


def key_for(client, who: dict | str, **extra) -> dict:
    user_id = who if isinstance(who, str) else who["id"]
    made = admin(client, "POST", "/admin/api-keys",
                 json={"user_id": user_id, "name": "script", **extra})
    assert made.status_code == 201, made.text
    return made.json()


def sign_in(client, username: str):
    client.cookies.clear()
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text


def call(client, key: str, method: str, path: str, **kw):
    client.cookies.clear()
    return client.request(method, path, headers=auth(key), **kw)


def catalogue(client, key: str) -> set[str]:
    return {m["id"] for m in call(client, key, "GET", "/v1/models").json()["data"]}


def stored(client, key_id: str) -> ApiKey:
    async def read():
        async with session_scope() as session:
            return await session.get(ApiKey, key_id)

    return client.portal.call(read)


def workspace(client, code: str, models: list[str]) -> dict:
    made = admin(client, "POST", "/admin/workspaces", json={"code": code, "name": code}).json()
    admin(client, "POST", f"/admin/workspaces/{made['id']}/models", json={"models": models})
    return made


# เส้นทางตัวแทนของงานดูแลแต่ละชนิด · (วิธี, path, body, รหัสเมื่อสำเร็จ)
# ครึ่งบนเป็นของ admin เท่านั้น ครึ่งล่าง manager ก็เข้าได้
ADMIN_ONLY = [
    ("GET", "/admin/models", None, 200),                    # registry พร้อมชื่อ backend
    ("GET", "/admin/secrets", None, 200),
    ("GET", "/v1/health/endpoints", None, 200),
    ("POST", "/admin/registry/reload", None, 200),          # แก้ registry
    ("PATCH", "/admin/models/coding/enabled", {"enabled": False}, 200),
    ("POST", "/admin/users", {"external_id": "made-by-a-key"}, 201),
    ("POST", "/admin/quota-policies",
     {"scope": "global", "name": "by a key", "window": "day", "max_requests": 5}, 201),
]
MANAGER_TOO = [
    ("GET", "/admin/users", None, 200),
    ("GET", "/admin/api-keys", None, 200),
    ("GET", "/admin/workspaces", None, 200),
    ("GET", "/admin/usage/summary", None, 200),
    ("GET", "/admin/tools", None, 200),
]
ROUTES = ADMIN_ONLY + MANAGER_TOO


def _id(route) -> str:
    return f"{route[0]} {route[1]}"


# ── ตัว defect: ใบที่จำกัดโมเดลไว้ ของผู้ดูแล ─────────────────────────────────

@pytest.mark.parametrize("route", ROUTES, ids=_id)
def test_a_model_limited_key_of_an_admin_is_refused_on_admin_routes(client, route):
    method, path, body, _ok = route
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])

    response = call(client, limited["api_key"], method, path, json=body)

    assert response.status_code == 403, f"{method} {path} → {response.status_code}"
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


def test_a_refused_key_changed_nothing(client):
    """403 ต้องมาก่อนงาน ไม่ใช่หลังงาน — ดูผลที่ของจริง ไม่ใช่ที่รหัสตอบกลับ"""
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])["api_key"]

    for method, path, body, _ok in ROUTES:
        call(client, limited, method, path, json=body)

    users = {u["external_id"] for u in admin(client, "GET", "/admin/users").json()["data"]}
    assert "made-by-a-key" not in users
    assert "coding" in catalogue(client, client.admin_key), "โมเดลถูกปิดโดย key ที่ไม่มีสิทธิ์"
    policies = admin(client, "GET", "/admin/quota-policies").json()["data"]
    assert [p for p in policies if p["name"] == "by a key"] == []


def test_it_cannot_lift_the_list_written_on_itself(client):
    """ทางยกระดับที่สั้นที่สุด: PATCH ตัวเองให้รายการว่าง แล้วเรียกได้ทุกโมเดล"""
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])

    for wider in ([], ["coding", "gemma-vision"]):
        response = call(client, limited["api_key"], "PATCH",
                        f"/admin/api-keys/{limited['id']}", json={"models": wider})
        assert response.status_code == 403, response.text

    assert list(stored(client, limited["id"]).models) == ["coding"]
    assert catalogue(client, limited["api_key"]) == {"coding"}


def test_it_cannot_push_its_own_expiry_out(client):
    limited = key_for(client, owner_of(client, client.admin_key),
                      models=["coding"], expires_in_days=7)
    before = stored(client, limited["id"]).expires_at

    response = call(client, limited["api_key"], "PATCH",
                    f"/admin/api-keys/{limited['id']}", json={"days": None})

    assert response.status_code == 403
    assert stored(client, limited["id"]).expires_at == before


def test_it_cannot_mint_a_key_without_limits(client):
    """ออกใบใหม่ให้เจ้าของตัวเองโดยไม่ใส่รายการ = ได้ใบ admin เต็มตัวมาแทน"""
    owner = owner_of(client, client.admin_key)
    limited = key_for(client, owner, models=["coding"])

    response = call(client, limited["api_key"], "POST", "/admin/api-keys",
                    json={"user_id": owner, "name": "replacement"})

    assert response.status_code == 403
    assert "api_key" not in response.json()
    names = {k["name"] for k in admin(client, "GET", "/admin/api-keys").json()["data"]}
    assert "replacement" not in names


def test_it_cannot_revoke_the_key_production_runs_on(client):
    owner = owner_of(client, client.admin_key)
    limited = key_for(client, owner, models=["coding"])
    production = key_for(client, owner)

    response = call(client, limited["api_key"], "DELETE",
                    f"/admin/api-keys/{production['id']}")

    assert response.status_code == 403
    assert call(client, production["api_key"], "GET", "/admin/users").status_code == 200


def test_it_cannot_reveal_somebody_elses_key(client, monkeypatch):
    """เปิดดู key คนอื่นคืออำนาจที่แรงที่สุดในหน้า Access — และทุกครั้งต้องมีแถวใน audit"""
    from app import config as config_mod

    # ตั้ง secret หลัง client ไม่ได้ผลกับ app ที่สร้างไปแล้ว แต่ seal()/unseal() อ่าน
    # settings ตอนเรียก จึงล้างแคชแล้วใช้ได้ (ดู fixture `sealed` ใน test_key_reveal.py)
    monkeypatch.setenv("GW_KEY_REVEAL_SECRET", "test-reveal-secret-not-a-real-one")
    config_mod.get_settings.cache_clear()
    try:
        somebody = person(client, "s1", "member")
        theirs = key_for(client, somebody)
        assert theirs["revealable"], "เทสนี้ต้องรันกับ reveal ที่เปิดอยู่จริง"
        limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])

        response = call(client, limited["api_key"], "POST",
                        f"/admin/api-keys/{theirs['id']}/reveal")

        assert response.status_code == 403
        assert theirs["api_key"] not in response.text

        async def reveals():
            async with session_scope() as session:
                return list((await session.execute(
                    select(AuditLog).where(AuditLog.action == "apikey.reveal")
                )).scalars())

        assert client.portal.call(reveals) == []
        # ใบที่ไม่มีข้อจำกัดของเจ้าของคนเดียวกัน ยังเปิดดูได้ตามเดิม
        opened = admin(client, "POST", f"/admin/api-keys/{theirs['id']}/reveal")
        assert opened.status_code == 200 and opened.json()["api_key"] == theirs["api_key"]
    finally:
        config_mod.get_settings.cache_clear()


def test_it_calls_its_own_model_and_nothing_else(client):
    """รายการบนใบใช้กับผู้ดูแลด้วย — ข้อนี้ผ่านอยู่แล้วก่อนแก้ ตรึงไว้ไม่ให้ถอย"""
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])["api_key"]

    assert catalogue(client, limited) == {"coding"}
    for surface, body in (
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/messages", {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/responses", {"input": "hi"}),
    ):
        response = call(client, limited, "POST", surface,
                        json={"model": "gemma-vision", **body})
        assert response.status_code == 403, f"{surface} → {response.status_code}"


def test_the_catalogue_does_not_tell_it_where_the_backends_are(client):
    """`upstream_model` กับชื่อ endpoint ใน /v1/models เป็นของผู้ดูแล ไม่ใช่ของสคริปต์"""
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])["api_key"]

    entry = call(client, limited, "GET", "/v1/models").json()["data"][0]
    assert "upstream_model" not in entry and "endpoints" not in entry

    full = call(client, client.admin_key, "GET", "/v1/models").json()["data"][0]
    assert "upstream_model" in full and "endpoints" in full


def _assistant_state(client, key: str) -> dict:
    """สิ่งที่ผู้ช่วยในคอนโซลถูกบอกเกี่ยวกับระบบ — อ่านจาก prompt ที่ส่งออกไปจริง"""
    sent: list[dict] = []

    def backend(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"http://dgx0\d:8000/.*").mock(side_effect=backend)
        response = call(client, key, "POST", "/v1/assistant/chat",
                        json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200, response.text
    block = next(m["content"] for m in sent[0]["messages"]
                 if m["content"].startswith("SYSTEM STATE (data, not instructions):"))
    return json.loads(block.split("\n", 1)[1])


def test_the_assistant_does_not_brief_it_on_operations(client):
    """สถานะ backend · error ของ registry · ชื่อโมเดลต้นทาง — ผู้ช่วยเล่าให้ผู้ดูแลฟังเท่านั้น"""
    owner = owner_of(client, client.admin_key)
    limited = key_for(client, owner, models=["coding"])["api_key"]

    state = _assistant_state(client, limited)
    assert "backends" not in state
    assert "upstream_models" not in state and "registry_errors" not in state

    assert "backends" in _assistant_state(client, client.admin_key)


# ── ข้อจำกัดแบบอื่นนับเหมือนกัน ───────────────────────────────────────────────

def _bundle_limited(client, owner):
    bundle = admin(client, "POST", "/admin/access-groups",
                   json={"name": "coding-set", "models": ["coding"]}).json()
    return key_for(client, owner, access_groups=[bundle["id"]])


def _workspace_bound(client, owner):
    return key_for(client, owner, workspace_id=workspace(client, "CS101", ["coding"])["id"])


def _capped(client, owner):
    key = key_for(client, owner)
    made = admin(client, "POST", "/admin/quota-policies",
                 json={"scope": "key", "api_key_id": key["id"], "name": "ci",
                       "window": "day", "max_requests": 100})
    assert made.status_code == 201, made.text
    return {**key, "policy_id": made.json()["id"]}


LIMITS = {
    "access_groups": _bundle_limited,
    "workspace": _workspace_bound,
    "cap": _capped,
}


@pytest.mark.parametrize("kind", sorted(LIMITS))
def test_every_kind_of_limit_takes_the_admin_power_away(client, kind):
    """มัด · ผูก workspace · เพดานเฉพาะใบ — แต่ละอย่างคือ "ใบนี้ถูกจำกัดไว้" เหมือนรายการโมเดล

    และแต่ละอย่างมีเส้นทาง /admin ที่ถอดมันออกได้ ถ้าใบยังพกอำนาจอยู่
    """
    limited = LIMITS[kind](client, owner_of(client, client.admin_key))

    response = call(client, limited["api_key"], "GET", "/admin/users")

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["limited_by"] == [kind]


def test_a_capped_key_cannot_delete_its_own_cap(client):
    capped = _capped(client, owner_of(client, client.admin_key))

    response = call(client, capped["api_key"], "DELETE",
                    f"/admin/quota-policies/{capped['policy_id']}")

    assert response.status_code == 403
    policies = admin(client, "GET", "/admin/quota-policies").json()["data"]
    assert capped["policy_id"] in {p["id"] for p in policies}


def _set_policy(client, policy_id: str, **values) -> None:
    async def write():
        async with session_scope() as session:
            policy = await session.get(QuotaPolicy, policy_id)
            for name, value in values.items():
                setattr(policy, name, value)

    client.portal.call(write)


@pytest.mark.parametrize("state, values", [
    ("in force", {}),
    ("switched off", {"enabled": False}),
    ("lapsed", {"expires_at": utcnow() - timedelta(hours=1)}),
    ("not yet lapsed", {"expires_at": utcnow() + timedelta(hours=1)}),
])
def test_a_cap_counts_exactly_while_the_quota_gate_enforces_it(client, state, values):
    """สองที่ที่ตอบคำถาม "ใบนี้มีเพดานไหม" ต้องตอบตรงกันเสมอ

    เพดานที่ปิดไปแล้วหรือหมดอายุแล้วไม่ได้จำกัดอะไร — นับมันเป็นข้อจำกัดคือถอดอำนาจ
    ของใบที่ไม่มีอะไรจำกัดอยู่จริง
    """
    capped = _capped(client, owner_of(client, client.admin_key))
    _set_policy(client, capped["policy_id"], **values)

    async def enforced() -> bool:
        async with session_scope() as session:
            limits = await client.app.state.services.quota.resolve_key_limits(
                session, capped["id"])
            return limits is not None

    refused = call(client, capped["api_key"], "GET", "/admin/users").status_code == 403
    assert refused == client.portal.call(enforced), state


def test_taking_the_limit_off_from_the_console_gives_the_power_back(client):
    """ทางออกที่ข้อความปฏิเสธบอกไว้ต้องเดินได้จริง — ไม่ต้องออกใบใหม่"""
    limited = key_for(client, owner_of(client, client.admin_key), models=["coding"])
    assert call(client, limited["api_key"], "GET", "/admin/users").status_code == 403

    lifted = admin(client, "PATCH", f"/admin/api-keys/{limited['id']}", json={"models": []})
    assert lifted.status_code == 200, lifted.text

    assert call(client, limited["api_key"], "GET", "/admin/users").status_code == 200


def test_scopes_are_not_a_limit(client):
    """`scopes` ถูกเก็บแต่ไม่เคยถูกบังคับที่ไหน และใบ bootstrap ของทุกเครื่องมี ["admin"]

    นับมันเป็นข้อจำกัดคือถอดอำนาจของใบที่ผู้ดูแลทุกคนถืออยู่ในวันที่อัปเกรด
    """
    key = key_for(client, owner_of(client, client.admin_key), scopes=["admin"])
    assert call(client, key["api_key"], "GET", "/admin/users").status_code == 200


# ── สิ่งที่ต้องไม่เปลี่ยน ──────────────────────────────────────────────────────

@pytest.mark.parametrize("route", ROUTES, ids=_id)
def test_an_admins_key_without_limits_works_as_before(client, route):
    """ใบที่สคริปต์ดูแลระบบใช้อยู่ทุกวัน — ออกโดยไม่ใส่ข้อจำกัดอะไรเลย"""
    method, path, body, ok = route
    plain = key_for(client, owner_of(client, client.admin_key))

    response = call(client, plain["api_key"], method, path, json=body)

    assert response.status_code == ok, response.text


@pytest.mark.parametrize("route", ROUTES, ids=_id)
def test_the_same_admin_signed_in_to_the_console_works_as_before(client, route):
    """session ไม่เคยถูกนับว่าจำกัด แม้เจ้าของจะมี key ที่จำกัดอยู่ในมือ"""
    method, path, body, ok = route
    root = person(client, "root", "admin")
    key_for(client, root, models=["coding"])
    sign_in(client, "root")

    response = client.request(method, path, json=body)

    assert response.status_code == ok, response.text


def test_the_bootstrap_key_of_a_fresh_install_is_still_an_admin(temp_db, monkeypatch):
    """ใบแรกของทุกเครื่องถูกสร้างพร้อม scopes=["admin"] โดย `_bootstrap_admin` ของจริง"""
    from fastapi.testclient import TestClient

    from app import config as config_mod
    from app.main import create_app

    bootstrap = "lg_sk_" + "b" * 43
    monkeypatch.setenv("GW_BOOTSTRAP_ADMIN_KEY", bootstrap)
    config_mod.get_settings.cache_clear()

    with TestClient(create_app()) as fresh:
        response = fresh.get("/admin/users", headers=auth(bootstrap))
        assert response.status_code == 200, response.text
        made = fresh.post("/admin/users", headers=auth(bootstrap),
                          json={"external_id": "first-person"})
        assert made.status_code == 201, made.text


def test_a_members_limited_key_is_told_what_it_was_always_told(client):
    """สมาชิกไม่มีอำนาจให้เสีย — ข้อความเรื่อง "ใบนี้ถูกจำกัด" จะพาเขาไปผิดทาง"""
    member = person(client, "s1", "member")
    limited = key_for(client, member, models=["coding"])["api_key"]

    response = call(client, limited, "GET", "/admin/users")

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["message"] == "Manager privileges are required."
    assert "limited_by" not in (error.get("details") or {})


# ── manager ──────────────────────────────────────────────────────────────────

def _manager_with_a_class(client):
    boss = person(client, "lecturer", "manager")
    cs101 = workspace(client, "CS101", ["coding", "gemma-vision"])
    joined = admin(client, "POST", f"/admin/workspaces/{cs101['id']}/join",
                   json={"user_id": boss["id"]})
    assert joined.status_code < 300, joined.text
    return boss, cs101


@pytest.mark.parametrize("route", MANAGER_TOO, ids=_id)
def test_a_managers_limited_key_loses_the_manager_routes(client, route):
    method, path, body, ok = route
    boss, _cs101 = _manager_with_a_class(client)
    limited = key_for(client, boss, models=["coding"])["api_key"]
    plain = key_for(client, boss)["api_key"]

    assert call(client, limited, method, path, json=body).status_code == 403
    # ใบที่ไม่มีข้อจำกัดของ manager คนเดียวกัน และตัวเขาเองในคอนโซล ทำได้ตามเดิม
    assert call(client, plain, method, path, json=body).status_code == ok
    sign_in(client, "lecturer")
    assert client.request(method, path, json=body).status_code == ok


def test_a_managers_limited_key_cannot_issue_or_widen_keys(client):
    """manager ออก key ให้คนในกลุ่มตัวเองได้ — ใบที่จำกัดไว้ของเขาต้องทำไม่ได้"""
    boss, cs101 = _manager_with_a_class(client)
    student = person(client, "s1", "member")
    admin(client, "POST", f"/admin/workspaces/{cs101['id']}/join",
          json={"user_id": student["id"]})
    limited = key_for(client, boss, models=["coding"])

    minted = call(client, limited["api_key"], "POST", "/admin/api-keys",
                  json={"user_id": student["id"], "name": "from a limited key"})
    widened = call(client, limited["api_key"], "PATCH",
                   f"/admin/api-keys/{limited['id']}",
                   json={"models": ["coding", "gemma-vision"]})

    assert minted.status_code == 403 and widened.status_code == 403
    assert catalogue(client, limited["api_key"]) == {"coding"}
    # ใบที่ไม่มีข้อจำกัดของเขายังออก key ให้นักเรียนในกลุ่มได้
    plain = key_for(client, boss)["api_key"]
    assert call(client, plain, "POST", "/admin/api-keys",
                json={"user_id": student["id"], "name": "ok"}).status_code == 201


# ── คำปฏิเสธต้องบอกว่าทำไม และให้ทำอะไรต่อ ─────────────────────────────────────

@pytest.mark.parametrize("role, path", [
    ("admin", "/admin/models"),
    ("admin", "/admin/users"),
    ("manager", "/admin/users"),
])
def test_the_refusal_says_why_and_what_to_do(client, role, path):
    who = person(client, f"the-{role}", role)
    limited = key_for(client, who, models=["coding"])["api_key"]

    error = call(client, limited, "GET", path).json()["error"]

    assert error["code"] == "INSUFFICIENT_SCOPE"
    message = error["message"]
    assert "model list" in message, "ต้องบอกว่าใบนี้ถูกจำกัดด้วยอะไร"
    assert "console" in message, "ทางออกที่หนึ่ง: ลงชื่อเข้าคอนโซล"
    assert "without" in message, "ทางออกที่สอง: ใช้ใบที่ออกโดยไม่มีข้อจำกัด"
    assert error["details"] == {
        "reason_code": "restricted_key", "limited_by": ["models"], "owner_role": role,
    }


def test_the_key_can_read_that_about_itself(client):
    """/v1/me/key คือที่ที่ใบหนึ่งถามว่า "ฉันทำอะไรได้" — ต้องตอบเรื่องนี้ด้วย"""
    owner = owner_of(client, client.admin_key)
    limited = key_for(client, owner, models=["coding"])["api_key"]

    mine = call(client, limited, "GET", "/v1/me/key").json()["key"]
    assert mine["limited_by"] == ["models"]
    assert mine["admin_access"] is False

    plain = call(client, client.admin_key, "GET", "/v1/me/key").json()["key"]
    assert plain["limited_by"] == []
    assert plain["admin_access"] is True


# ── จุดเดียวที่ตัดสิน ──────────────────────────────────────────────────────────

def _principal(**kw):
    from app.core.auth import Principal

    base = dict(user_id="u", external_id="x", role="admin", display_name="",
                api_key_id="k", workspace_id=None, scopes=[])
    return Principal(**{**base, **kw})


@pytest.mark.parametrize("limit, name", [
    ({"key_models": ["coding"]}, "models"),
    ({"key_access_groups": ["g1"]}, "access_groups"),
    ({"workspace_id": "w1"}, "workspace"),
    ({"key_capped": True}, "cap"),
])
def test_the_privilege_is_decided_in_one_place(limit, name):
    """เส้นทางใหม่ที่เขียน `if principal.is_admin` ต้องได้คำตอบที่ถูกเองโดยไม่ต้องจำอะไร"""
    for role in ("admin", "manager"):
        limited = _principal(role=role, **limit)
        assert limited.key_limits == (name,)
        assert not limited.is_admin and not limited.is_manager

    # คนเดียวกันในคอนโซล: ข้อมูลชุดเดียวกันบน Principal ไม่ทำให้ session ถูกจำกัด
    signed_in = _principal(via="session", **limit)
    assert signed_in.key_limits == () and signed_in.is_admin


def test_a_limited_key_does_not_skip_the_scope_check():
    """`require_scope` ยกเว้นให้ admin — ข้อยกเว้นนั้นเป็นอำนาจ ต้องหายไปพร้อมกัน"""
    from app.core.errors import GatewayError

    _principal(scopes=["chat"]).require_scope("embeddings")          # admin เต็มตัว: ผ่าน
    with pytest.raises(GatewayError):
        _principal(scopes=["chat"], key_models=["coding"]).require_scope("embeddings")


def test_only_keys_that_could_lose_something_pay_for_the_cap_lookup(client, monkeypatch):
    """เพดานเฉพาะใบอยู่คนละตาราง = query เพิ่มหนึ่งตัวใน `authenticate` ซึ่งอยู่บนทางเดินของทุกคำขอ

    วัด "จำนวนครั้ง" ไม่ใช่ผล (แบบเดียวกับ test_hot_path_writes.py): คำขอของสมาชิกต้อง
    ไม่เสีย query นี้เลย และใบที่ถูกนับว่าจำกัดไปแล้วด้วยเหตุอื่นก็ไม่ต้องถามซ้ำ
    """
    from app.core import auth as auth_mod

    asked: list[str] = []
    real = auth_mod.has_cap_in_force

    async def counting(session, api_key_id):
        asked.append(api_key_id)
        return await real(session, api_key_id)

    monkeypatch.setattr(auth_mod, "has_cap_in_force", counting)
    owner = owner_of(client, client.admin_key)
    member = key_for(client, person(client, "s1", "member"))
    plain, narrowed = key_for(client, owner), key_for(client, owner, models=["coding"])
    asked.clear()

    for key in (member, narrowed, plain):
        assert call(client, key["api_key"], "GET", "/v1/models").status_code == 200

    assert asked == [plain["id"]]
