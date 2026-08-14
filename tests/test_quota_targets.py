"""Giving a quota policy a name, and a set of models to apply to.

Two things an operator asked for after living with the screen: a policy has no
name, so six of them are told apart only by reading their scope and target; and
one policy covers one model, so a limit meant for the four coding models was
four policies to write and four to remember to change.

The set already exists — an access group is a named list of aliases — so a
policy points at one rather than growing a second list of models that would have
to be kept in step with the first.
"""

from __future__ import annotations

import httpx
import pytest
import respx

UPSTREAM = "http://dgx03:8000/v1/chat/completions"
VISION = "http://dgx02:8000/v1/chat/completions"

REPLY = {
    "id": "c1", "object": "chat.completion", "model": "m",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


@pytest.fixture
def upstream():
    with respx.mock:
        respx.post(UPSTREAM).mock(return_value=httpx.Response(200, json=REPLY))
        respx.post(VISION).mock(return_value=httpx.Response(200, json=REPLY))
        yield


def bundle(client, name, models):
    return client.post("/admin/access-groups", headers=auth(client.admin_key),
                       json={"name": name, "models": list(models)}).json()


def policy(client, **body):
    return client.post("/admin/quota-policies", headers=auth(client.admin_key),
                       json={"scope": "global", "window": "day", **body})


def ask(client, key, alias):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": alias, "messages": [{"role": "user", "content": "hi"}]})


def test_a_policy_can_be_given_a_name(client):
    """หกใบที่แยกกันได้ด้วยการอ่าน scope อย่างเดียว คือหกใบที่ไม่มีใครกล้าแตะ"""
    created = policy(client, name="ช่วงสอบปลายภาค", max_requests=50)
    assert created.status_code == 201

    listed = client.get("/admin/quota-policies", headers=auth(client.admin_key)).json()["data"]
    assert any(p["name"] == "ช่วงสอบปลายภาค" for p in listed)


def test_one_policy_can_cover_a_whole_bundle(client, member_key, upstream):
    coding = bundle(client, "coding-set", ["coding", "gemma-vision"])
    policy(client, access_group_id=coding["id"], max_requests_per_minute=1)

    assert ask(client, member_key, "coding").status_code == 200
    refused = ask(client, member_key, "gemma-vision")
    assert refused.status_code == 429, "โมเดลที่สองในมัดเดียวกันต้องนับรวมกัน"


def test_a_model_outside_the_bundle_is_untouched(client, member_key, upstream):
    coding = bundle(client, "coding-set", ["coding"])
    policy(client, access_group_id=coding["id"], max_requests_per_minute=1)

    assert ask(client, member_key, "coding").status_code == 200
    assert ask(client, member_key, "gemma-vision").status_code == 200


def test_a_named_model_beats_a_bundle_containing_it(client, member_key, upstream):
    """กฎที่เจาะจงกว่าชนะ · เขียนถึงโมเดลตัวเดียวคือเขียนโดยรู้เคสมากกว่า"""
    coding = bundle(client, "coding-set", ["coding"])
    policy(client, access_group_id=coding["id"], max_requests_per_minute=1)
    policy(client, model_alias="coding", max_requests_per_minute=5)

    for _ in range(3):
        assert ask(client, member_key, "coding").status_code == 200


def test_a_bundle_beats_a_policy_with_no_target(client, member_key, upstream):
    coding = bundle(client, "coding-set", ["coding"])
    policy(client, max_requests_per_minute=5)
    policy(client, access_group_id=coding["id"], max_requests_per_minute=1)

    assert ask(client, member_key, "coding").status_code == 200
    assert ask(client, member_key, "coding").status_code == 429


def test_naming_both_a_model_and_a_bundle_is_refused(client):
    """สองคำตอบต่อคำถามเดียวคือกติกาที่ไม่มีใครเดาผลได้"""
    coding = bundle(client, "coding-set", ["coding"])
    response = policy(client, model_alias="coding", access_group_id=coding["id"])
    assert response.status_code == 400
    assert "not both" in response.json()["error"]["message"]


def test_a_bundle_that_does_not_exist_is_refused(client):
    assert policy(client, access_group_id="nope").status_code == 400


def test_a_disabled_bundle_stops_applying(client, member_key, upstream):
    """ปิดมัดต้องหยุดทุกอย่างที่อ้างถึงมัน ไม่ใช่แค่สิทธิ์"""
    coding = bundle(client, "coding-set", ["coding"])
    policy(client, access_group_id=coding["id"], max_requests_per_minute=1)
    client.patch(f"/admin/access-groups/{coding['id']}",
                 headers=auth(client.admin_key), json={"enabled": False})

    for _ in range(3):
        assert ask(client, member_key, "coding").status_code == 200
