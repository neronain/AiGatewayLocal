"""ผู้ช่วยในคอนโซลเลือกและเสนอได้เฉพาะโมเดลที่ผู้เรียก "เรียกได้จริง"

เคสจริง (ตรวจ 2026-10-06 บน a5e9295): key ที่จำกัดไว้ที่ `gemma-vision` ถาม
`/v1/assistant/status` ได้ `available: true, model: "coding"` แล้วทุกข้อความที่ส่งไป
`/v1/assistant/chat` ได้ 403 MODEL_NOT_PERMITTED — เพราะผู้ช่วยเลือกโมเดลจาก "ที่ role
มองเห็น" โดยไม่ถามด่านสิทธิ์ (workspace · กลุ่มของเจ้าของ · รายการบน key) ส่วนท่อที่มัน
ส่งคำขอผ่านถาม · ไม่ใช่ทางลัดข้ามกติกา แต่เป็นกล่องแชตที่บอกว่าใช้ได้ทั้งที่ใช้ไม่ได้

และ `models_i_can_use` ที่ใส่ลงใน prompt ก็ไม่ได้กรองเหมือนกัน: โมเดลตอบคำถาม "ฉันใช้
ตัวไหนได้บ้าง" จากรายการนั้น จึงตอบชื่อที่ผู้ถามจะถูกปฏิเสธ
"""

from __future__ import annotations

import json

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


def person(client, external_id="narrow"):
    return admin(client, "POST", "/admin/users", json={"external_id": external_id}).json()


def key_for(client, who, **extra) -> str:
    made = admin(client, "POST", "/admin/api-keys", json={"user_id": who["id"], **extra})
    assert made.status_code == 201, made.text
    return made.json()["api_key"]


def status(client, key):
    return client.get("/v1/assistant/status", headers=auth(key)).json()


def chat(client, key):
    """ส่งหนึ่งข้อความ · คืน (response, คำขอที่ไปถึง backend) — ไม่มีคำขอ = ไม่เคยไปถึง"""
    sent: list[dict] = []

    def backend(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"http://dgx0\d:8000/.*").mock(side_effect=backend)
        response = client.post("/v1/assistant/chat", headers=auth(key),
                               json={"messages": [{"role": "user", "content": "hello"}]})
    return response, sent


def offered(sent: list[dict]) -> list[str]:
    """alias ที่ผู้ช่วยบอกโมเดลว่าผู้เรียกใช้ได้ — อ่านจาก prompt ที่ส่งออกไปจริง"""
    block = next(m["content"] for m in sent[0]["messages"]
                 if m["content"].startswith("SYSTEM STATE (data, not instructions):"))
    state = json.loads(block.split("\n", 1)[1])
    return [m["alias"] for m in state["models_i_can_use"]]


# ── ตัว defect ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("only", sorted(ALL))
def test_a_key_limited_to_one_model_gets_that_model_or_nothing(client, only):
    """สิ่งที่ status บอกกับสิ่งที่ chat ทำ ต้องเป็นเรื่องเดียวกันเสมอ"""
    key = key_for(client, person(client), models=[only])

    said = status(client, key)
    response, sent = chat(client, key)

    if said["available"]:
        assert said["model"] == only, "เสนอโมเดลที่ key ใบนี้เรียกไม่ได้"
        assert response.status_code == 200, response.text
        assert offered(sent) == [only]
    else:
        assert said["model"] is None and said["reason"]
        assert response.status_code == 404
        assert sent == []


def test_the_prompt_lists_only_what_the_caller_may_call(client):
    who = person(client)
    ws = admin(client, "POST", "/admin/workspaces", json={"code": "CS101", "name": "x"}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/models",
          json={"models": ["coding", "muse-local"]})
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/join", json={"user_id": who["id"]})

    response, sent = chat(client, key_for(client, who))

    assert response.status_code == 200, response.text
    assert sorted(offered(sent)) == ["coding", "muse-local"]


def test_someone_unrestricted_is_offered_everything_as_before(client, member_key):
    said = status(client, member_key)
    response, sent = chat(client, member_key)
    assert said["available"] is True
    assert response.status_code == 200
    assert set(offered(sent)) == ALL


# ── ไม่มีโมเดลให้ใช้ ต้องบอกว่าไม่มี และบอกว่าเพราะอะไร ──────────────────────

def test_nothing_permitted_means_unavailable_with_the_rule_that_limits_them(client):
    """อยู่ใน workspace ที่ยังไม่เปิดโมเดลสักตัว — เดิม status บอก available: true"""
    who = person(client)
    ws = admin(client, "POST", "/admin/workspaces", json={"code": "EMPTY", "name": "x"}).json()
    admin(client, "POST", f"/admin/workspaces/{ws['id']}/join", json={"user_id": who["id"]})
    key = key_for(client, who)

    said = status(client, key)
    response, sent = chat(client, key)

    assert said["available"] is False
    assert said["model"] is None
    assert "the workspaces you belong to" in said["reason"]
    assert response.status_code == 404
    assert response.json()["error"]["message"] == said["reason"]
    assert sent == [], "ต้องไม่ส่งอะไรไปถึง backend"


def test_a_pinned_model_the_caller_may_not_call_is_reported_not_replaced(client):
    """ผู้ดูแลปักหมุดผู้ช่วยไว้ที่ coding · key ใบนี้เรียก coding ไม่ได้

    ไม่เลือกตัวอื่นให้เงียบ ๆ — นั่นคือการซ่อนปัญหาสิทธิ์ไว้หลังกล่องแชตที่ดูเหมือนใช้ได้
    """
    pinned = admin(client, "PUT", "/admin/assistant", json={"alias": "coding"})
    assert pinned.status_code == 200, pinned.text
    key = key_for(client, person(client), models=["gemma-vision"])

    said = status(client, key)
    response, sent = chat(client, key)

    assert said["available"] is False
    assert said["pinned"] is True
    assert "pinned to 'coding'" in said["reason"]
    assert response.status_code == 404
    assert sent == []


def test_a_pinned_model_the_caller_may_call_is_used(client):
    admin(client, "PUT", "/admin/assistant", json={"alias": "coding"})
    key = key_for(client, person(client), models=["coding", "gemma-vision"])

    assert status(client, key)["model"] == "coding"
    response, sent = chat(client, key)
    assert response.status_code == 200
    assert sorted(offered(sent)) == ["coding", "gemma-vision"]
