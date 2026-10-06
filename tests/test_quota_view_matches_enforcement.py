"""หน้าจอโควตาและปุ่มคืนโควตาต้องพูดถึงกฎเดียวกับที่ทางเดินของคำขอบังคับ

ตรวจพบ 2026-10-06: สมาชิกที่ติดนโยบายของ workspace "1 ครั้งต่อเดือน" —

* คอนโซลบอกว่าเขาอยู่ใต้ `{'source': 'default', 'window': 'day'} max_requests=500`
* กด Reset quota → 200 `{'window': 'day', 'cleared': {…}}` แล้วเขายัง 429 จากนโยบายรายเดือน
* เพดานของ key (`key:<id>`) ไม่มีทางถูกล้างจากหน้าจอเลย

หน้าจอถามแค่ชุดเดียว — "ไม่มี workspace ไม่มีโมเดล" — ทั้งที่คำขอจริงถูกตัดสินด้วย
workspace ของ key และโมเดลที่เรียก · หน้าจอบอกว่าผ่าน ไม่ใช่หลักฐานว่าผ่าน
"""

from __future__ import annotations

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


def admin(client, method, path, **kw):
    return client.request(method, path, headers=auth(client.admin_key), **kw)


def ask(client, key, model="coding"):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def person(client, external_id="stu1"):
    return admin(client, "POST", "/admin/users", json={"external_id": external_id}).json()


def issue(client, who, **extra):
    return admin(client, "POST", "/admin/api-keys",
                 json={"user_id": who["id"], "name": "k", **extra}).json()


def workspace(client, who, code="CS101"):
    ws = admin(client, "POST", "/admin/workspaces", json={"code": code, "name": code}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/models", json={"models": ["coding"]})
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/join", json={"user_id": who["id"]})
    return ws


def policy(client, **body):
    made = admin(client, "POST", "/admin/quota-policies", json={"window": "day", **body})
    assert made.status_code == 201, made.text
    return made.json()


def shown(client, who) -> dict:
    return admin(client, "GET", f"/admin/users/{who['id']}/quota").json()


def lane(view: dict, **want) -> dict:
    """ชุดลิมิตในหน้าจอที่ตรงกับเงื่อนไข — ต้องมีหนึ่งเดียว"""
    def matches(p):
        return all(p.get(k, p["applies_to"].get(k)) == v for k, v in want.items())
    found = [p for p in view["policies"] if matches(p)]
    assert len(found) == 1, (want, view["policies"])
    return found[0]


def reset(client, who, **body):
    return admin(client, "POST", f"/admin/users/{who['id']}/quota/reset",
                 json=body if body else None)


# ---------------------------------------------------------------------------
# เคสที่ตรวจพบ: นโยบายของ workspace
# ---------------------------------------------------------------------------
@pytest.fixture
def cs101(client):
    """สมาชิกของ CS101 ที่ใช้โควตาของวิชาหมดแล้ว และมี key ส่วนตัวอีกใบที่ใช้ไปสามครั้ง"""
    who = person(client)
    ws = workspace(client, who)
    rule = policy(client, scope="workspace", workspace_id=ws["id"], window="month",
                  max_requests=1, name="CS101: 1 request a month")
    bound = issue(client, who, workspace_id=ws["id"], name="bound")
    loose = issue(client, who, name="no workspace picked")

    assert ask(client, bound["api_key"]).status_code == 200
    assert ask(client, bound["api_key"]).status_code == 429
    for _ in range(3):
        assert ask(client, loose["api_key"]).status_code == 200
    return {"who": who, "ws": ws, "rule": rule, "bound": bound, "loose": loose}


def test_the_view_shows_the_workspace_policy_that_is_stopping_them(client, cs101):
    view = shown(client, cs101["who"])

    blocking = lane(view, policy_id=cs101["rule"]["id"])
    assert blocking["source"] == "workspace"
    assert blocking["policy_name"] == "CS101: 1 request a month"
    assert blocking["window"] == "month"
    assert blocking["limits"]["max_requests"] == 1
    assert blocking["used"]["requests"] == 1, "การใช้งานของกองนี้เอง ไม่ใช่ของกองรวม"
    assert blocking["exhausted"] is True
    assert blocking["percent"] == 100
    assert blocking["applies_to"]["workspace_id"] == cs101["ws"]["id"]
    assert blocking["applies_to"]["workspace_code"] == "CS101"


def test_the_view_still_shows_what_binds_their_other_keys(client, cs101):
    view = shown(client, cs101["who"])

    own = lane(view, source="default")
    assert (own["window"], own["limits"]["max_requests"]) == ("day", 500)
    assert own["used"]["requests"] == 3
    assert own["exhausted"] is False
    # ฟิลด์เดิมของ endpoint นี้คือชุดเดียวกัน — ฟอร์มออก key ใช้เติมค่าให้ล่วงหน้า
    assert (view["source"], view["window"]) == ("default", "day")
    assert view["used"]["requests"] == 3
    assert view["policies"][0] == own


def test_the_view_says_which_keys_the_workspace_policy_does_not_bind(client, cs101):
    """สมาชิกของ workspace ที่มีนโยบาย ถือ key ที่ไม่ได้อยู่ใต้นโยบายนั้น — ต้องบอกตรง ๆ"""
    notes = shown(client, cs101["who"])["not_bound"]

    assert len(notes) == 1
    note = notes[0]
    assert note["policy_id"] == cs101["rule"]["id"]
    assert note["workspace_code"] == "CS101"
    assert [k["id"] for k in note["keys"]] == [cs101["loose"]["id"]]
    assert [k["name"] for k in note["keys"]] == ["no workspace picked"]
    assert "not" in note["reason"]


def test_a_member_with_no_key_for_the_workspace_is_flagged_not_shown_as_limited(client):
    """เป็นสมาชิก มีนโยบาย แต่ไม่มี key ใบไหนออกให้วิชานั้น = นโยบายไม่ผูกคำขอไหนของเขาเลย"""
    who = person(client)
    ws = workspace(client, who)
    rule = policy(client, scope="workspace", workspace_id=ws["id"], max_requests=1)
    loose = issue(client, who)

    view = shown(client, who)

    assert [p["policy_id"] for p in view["policies"]] == [""], "มีแค่ค่าตั้งต้น"
    assert [n["policy_id"] for n in view["not_bound"]] == [rule["id"]]
    for _ in range(3):
        assert ask(client, loose["api_key"]).status_code == 200, "และมันไม่ผูกจริง ๆ"


def test_reset_clears_the_counter_that_was_stopping_them(client, cs101):
    """เคสที่ตรวจพบ: reset ตอบ 200 แล้วสมาชิกยัง 429"""
    out = reset(client, cs101["who"])
    assert out.status_code == 200, out.text
    body = out.json()

    who, ws = cs101["who"]["id"], cs101["ws"]["id"]
    cleared = {(c["counter"], c["window"]): c["used"]["requests"]
               for c in body["counters_cleared"]}
    assert cleared == {(f"user:{who}", "day"): 3, (f"user:{who}:ws:{ws}", "month"): 1}
    assert body["not_cleared"] == []

    assert ask(client, cs101["bound"]["api_key"]).status_code == 200, "ต้องใช้ได้จริงหลังคืน"
    after = shown(client, cs101["who"])
    assert lane(after, policy_id=cs101["rule"]["id"])["used"]["requests"] == 1
    assert lane(after, source="default")["used"]["requests"] == 0


def test_the_old_reset_fields_are_still_there(client, cs101):
    """คอนโซลรุ่นที่ deploy อยู่อ่าน `cleared.requests` — ต้องยังได้ตัวเลขของกองเดิม"""
    body = reset(client, cs101["who"]).json()
    assert body["window"] == "day"
    assert body["cleared"]["requests"] == 3
    assert body["usage"]["used"]["requests"] == 0


def test_the_people_list_carries_every_binding_policy_too(client, cs101):
    rows = admin(client, "GET", "/admin/usage/quota").json()["data"]
    row = next(r for r in rows if r["user_id"] == cs101["who"]["id"])

    assert (row["source"], row["window"], row["used"]["requests"]) == ("default", "day", 3)
    assert lane(row, policy_id=cs101["rule"]["id"])["exhausted"] is True
    assert [n["policy_id"] for n in row["not_bound"]] == [cs101["rule"]["id"]]


# ---------------------------------------------------------------------------
# นโยบายที่เล็งโมเดล / มัด
# ---------------------------------------------------------------------------
def test_a_model_policy_is_shown_with_its_own_usage(client):
    who = person(client)
    key = issue(client, who)["api_key"]
    rule = policy(client, scope="user", user_id=who["id"], model_alias="coding",
                  max_requests=2, name="coding 2/day")
    ask(client, key, "coding")
    for _ in range(3):
        ask(client, key, "gemma-vision")

    view = shown(client, who)

    coding = lane(view, policy_id=rule["id"])
    assert coding["applies_to"]["model_alias"] == "coding"
    assert (coding["used"]["requests"], coding["limits"]["max_requests"]) == (1, 2)
    assert coding["percent"] == 50
    assert lane(view, source="default")["used"]["requests"] == 3


def test_a_bundle_policy_is_shown_once_however_many_models_it_covers(client):
    who = person(client)
    key = issue(client, who)["api_key"]
    group = admin(client, "POST", "/admin/access-groups",
                  json={"name": "both", "models": ["coding", "gemma-vision"]}).json()
    rule = policy(client, scope="global", access_group_id=group["id"], max_requests=10)
    ask(client, key, "coding")
    ask(client, key, "gemma-vision")

    view = shown(client, who)

    bundle = lane(view, policy_id=rule["id"])
    assert bundle["applies_to"]["access_group_id"] == group["id"]
    assert bundle["used"]["requests"] == 2
    assert len(view["policies"]) == 2, "ค่าตั้งต้น + มัด — ไม่ใช่หนึ่งแถวต่อโมเดล"


def test_reset_clears_a_model_policy_and_its_minute(client):
    who = person(client)
    key = issue(client, who)["api_key"]
    policy(client, scope="user", user_id=who["id"], model_alias="coding",
           max_requests=5, max_requests_per_minute=1)
    assert ask(client, key).status_code == 200
    assert ask(client, key).status_code == 429

    body = reset(client, who).json()

    assert f"user:{who['id']}:model:coding" in [c["counter"] for c in body["counters_cleared"]]
    assert ask(client, key).status_code == 200, "ลิมิตต่อนาทีของกองนั้นต้องถูกล้างด้วย"


# ---------------------------------------------------------------------------
# เพดานของ key
# ---------------------------------------------------------------------------
@pytest.fixture
def capped(client):
    who = person(client, "ci-owner")
    key = issue(client, who, name="ci-token")
    rule = policy(client, scope="key", api_key_id=key["id"], max_requests=1, name="ci: 1/day")
    assert ask(client, key["api_key"]).status_code == 200
    assert ask(client, key["api_key"]).status_code == 429
    return {"who": who, "key": key, "rule": rule}


def test_a_key_ceiling_is_shown_against_the_key_it_is_on(client, capped):
    ceiling = lane(shown(client, capped["who"]), policy_id=capped["rule"]["id"])

    assert ceiling["source"] == "key"
    assert ceiling["counter"] == f"key:{capped['key']['id']}"
    assert ceiling["applies_to"]["api_key_id"] == capped["key"]["id"]
    assert ceiling["applies_to"]["api_key_name"] == "ci-token"
    assert ceiling["exhausted"] is True


def test_reset_says_it_did_not_clear_the_key_ceiling(client, capped):
    """คืนโควตาของคน ≠ คืนเพดานของใบ — แต่ต้องบอก เพราะคนนี้ยังถูกปฏิเสธผ่านใบนั้นอยู่"""
    body = reset(client, capped["who"]).json()

    assert [c["counter"] for c in body["counters_cleared"]] == [f"user:{capped['who']['id']}"]
    (kept,) = body["not_cleared"]
    assert kept["counter"] == f"key:{capped['key']['id']}"
    assert kept["policy_id"] == capped["rule"]["id"]
    assert kept["exhausted"] is True
    assert "include_keys" in kept["reason"]
    assert ask(client, capped["key"]["api_key"]).status_code == 429, "ตามที่รายงาน: ยังไม่ได้ล้าง"


def test_reset_clears_the_key_ceiling_when_asked_to(client, capped):
    body = reset(client, capped["who"], include_keys=True).json()

    assert f"key:{capped['key']['id']}" in [c["counter"] for c in body["counters_cleared"]]
    assert body["not_cleared"] == []
    assert ask(client, capped["key"]["api_key"]).status_code == 200


def test_include_keys_must_be_a_boolean(client, capped):
    assert reset(client, capped["who"], include_keys="yes").status_code == 400


def test_a_revoked_keys_ceiling_is_not_listed(client, capped):
    """ใบที่ถูกเพิกถอนแล้วผูกคำขอไหนไม่ได้อีก — ไม่ควรโผล่ว่าเป็นสิ่งที่ยังกั้นเขาอยู่"""
    admin(client, "DELETE", f"/admin/api-keys/{capped['key']['id']}")
    view = shown(client, capped["who"])
    assert [p["source"] for p in view["policies"]] == ["default"]
    assert reset(client, capped["who"]).json()["not_cleared"] == []


def test_the_audit_entry_names_each_counter(client, cs101):
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope

    reset(client, cs101["who"])

    async def last():
        async with session_scope() as session:
            rows = (await session.execute(
                select(AuditLog).where(AuditLog.action == "quota.reset")
            )).scalars().all()
            return rows[-1].payload

    entry = client.portal.call(last)
    who, ws = cs101["who"]["id"], cs101["ws"]["id"]
    assert {(c["counter"], c["window"], c["used"]["requests"]) for c in entry["counters"]} == {
        (f"user:{who}", "day", 3), (f"user:{who}:ws:{ws}", "month", 1)}
    assert entry["cleared"]["requests"] == 3


# ---------------------------------------------------------------------------
# /v1/me — สิ่งที่สมาชิกเห็นเอง
# ---------------------------------------------------------------------------
def test_me_shows_the_policy_of_the_workspace_the_key_was_issued_for(client, cs101):
    mine = client.get("/v1/me", headers=auth(cs101["bound"]["api_key"])).json()

    assert mine["quota"]["window"] == "month"
    assert mine["quota"]["used"]["requests"] == 1
    binding = lane({"policies": mine["quota_policies"]}, policy_id=cs101["rule"]["id"])
    assert binding["exhausted"] is True


def test_me_shows_a_model_policy_and_this_keys_ceiling_but_not_another_keys(client):
    who = person(client)
    mine = issue(client, who, name="mine")
    other = issue(client, who, name="other")
    by_model = policy(client, scope="user", user_id=who["id"], model_alias="coding",
                      max_requests=9)
    my_ceiling = policy(client, scope="key", api_key_id=mine["id"], max_requests=4)
    policy(client, scope="key", api_key_id=other["id"], max_requests=7)

    seen = client.get("/v1/me", headers=auth(mine["api_key"])).json()["quota_policies"]

    assert {p["policy_id"] for p in seen} == {"", by_model["id"], my_ceiling["id"]}


# ---------------------------------------------------------------------------
# คอนโซล — ลูกค้าใช้ผ่าน GUI: ข้อมูลที่ API ส่งมาแต่หน้าจอไม่แสดง ถือว่ายังไม่ได้บอก
# ---------------------------------------------------------------------------
_RENDER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const from = src.indexOf('function quotaLaneLabel(');
const to = src.indexOf('async function loadAccess()');
if (from < 0 || to < from) throw new Error('quota cell functions not found in app.js');
const esc = (v) => String(v ?? '').replace(/[&<>"]/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const num = (v) => Number(v || 0).toLocaleString();
eval(src.slice(from, to) + '\nglobalThis.quotaCell = quotaCell;');
const row = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(quotaCell(row));
"""


def test_the_people_table_cell_names_the_policy_that_is_full(client, cs101, tmp_path):
    """รันฟังก์ชันจริงของคอนโซลกับแถวจริงจาก API — ไม่ใช่หาข้อความในซอร์ส"""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    rows = admin(client, "GET", "/admin/usage/quota").json()["data"]
    row = next(r for r in rows if r["user_id"] == cs101["who"]["id"])
    script = tmp_path / "render.js"
    script.write_text(_RENDER, encoding="utf-8")
    app_js = Path(__file__).resolve().parents[1] / "app/static/app.js"

    done = subprocess.run([node, str(script), str(app_js)], input=json.dumps(row),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    lines = [ln.strip() for ln in done.stdout.split("<div") if ln.strip()]

    # แถบหลักยังเป็นของเดิม: 3 จาก 500 ของค่าตั้งต้น
    assert any("3 / 500" in ln for ln in lines), done.stdout
    # และมีบรรทัดที่บอกว่านโยบายของวิชาเต็มแล้ว — ตัวที่หยุดเขาจริง
    full = [ln for ln in lines if "CS101: 1 request a month" in ln]
    assert len(full) == 1 and "เต็มแล้ว" in full[0] and "100%" in full[0], done.stdout
    # และบอกว่า key ใบไหนไม่อยู่ใต้นโยบายนั้น
    assert any("ไม่อยู่ใต้นโยบายของวิชา CS101" in ln and "no workspace picked" in ln
               for ln in lines), done.stdout
