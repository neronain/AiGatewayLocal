"""Changing how much work each machine behind an alias takes.

`priority`, `weight` and `max_concurrency` decide whether two backends share the
load or whether one is standby, and until now they could only be changed through
the full model editor — which rewrites the whole registry file and drops every
comment in it. That is a poor trade for turning one number.

The behaviour they control is in `app/core/routing.py`: the highest priority
tier that still has room takes everything, equal priority shares by least
in-flight, and a full tier spills to the one below.
"""

from __future__ import annotations

import pytest


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _writable(writable_config):
    return writable_config


def tune(client, alias, endpoint, **body):
    return client.patch(
        f"/admin/models/{alias}/endpoints/{endpoint}",
        headers=auth(client.admin_key), json=body,
    )


def endpoint_of(client, alias, name):
    models = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    model = next(m for m in models if m["alias"] == alias)
    return next(e for e in model["endpoints"] if e["name"] == name)


def test_priority_can_be_changed_on_its_own(client):
    assert tune(client, "coding", "dgx03", priority=50).status_code == 200
    assert endpoint_of(client, "coding", "dgx03")["priority"] == 50


def test_several_knobs_move_together(client):
    response = tune(client, "coding", "dgx03", priority=90, max_concurrency=32)
    assert response.status_code == 200
    assert response.json()["changed"] == {"priority": 90, "max_concurrency": 32}

    live = endpoint_of(client, "coding", "dgx03")
    assert (live["priority"], live["max_concurrency"]) == (90, 32)


def test_the_comments_in_the_file_survive(client, writable_config):
    """เหตุผลที่เขียนกำกับไว้ว่าทำไมตั้งค่าแบบนั้น สำคัญที่สุดตอนมีคนกำลังจะเปลี่ยนมัน"""
    path = writable_config / "models" / "coding.yaml"
    before = [line for line in path.read_text().splitlines() if line.strip().startswith("#")]
    assert before, "ไฟล์ตัวอย่างต้องมีคอมเมนต์ ไม่งั้นเทสนี้ไม่ได้ตรวจอะไร"

    tune(client, "coding", "dgx03", priority=42)

    after = path.read_text().splitlines()
    for line in before:
        assert line in after, f"คอมเมนต์หาย: {line.strip()}"


def test_a_value_out_of_range_is_refused_and_changes_nothing(client):
    before = endpoint_of(client, "coding", "dgx03")["max_concurrency"]
    response = tune(client, "coding", "dgx03", max_concurrency=0)

    assert response.status_code == 400
    assert "between" in response.json()["error"]["message"]
    assert endpoint_of(client, "coding", "dgx03")["max_concurrency"] == before


def test_something_that_is_not_a_number_says_so(client):
    response = tune(client, "coding", "dgx03", priority="high")
    assert response.status_code == 400
    assert "whole number" in response.json()["error"]["message"]


def test_an_empty_body_says_what_it_accepts(client):
    response = tune(client, "coding", "dgx03")
    assert response.status_code == 400
    assert "priority" in response.json()["error"]["message"]


def test_an_unknown_endpoint_says_so(client):
    response = tune(client, "coding", "not-a-machine", priority=10)
    assert response.status_code == 400
    assert "not-a-machine" in response.json()["error"]["message"]


def test_an_unknown_alias_says_so(client):
    assert tune(client, "not-a-model", "dgx03", priority=10).status_code in (400, 404)


def test_a_member_cannot_retune_the_fleet(client, member_key):
    response = client.patch(
        "/admin/models/coding/endpoints/dgx03",
        headers=auth(member_key), json={"priority": 1},
    )
    assert response.status_code in (401, 403)
    assert endpoint_of(client, "coding", "dgx03")["priority"] == 100
