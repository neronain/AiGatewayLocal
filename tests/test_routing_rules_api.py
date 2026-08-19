"""Routing rules through the real request path, not just the rules module.

The module tests prove the decision. These prove the wiring: that the decision
actually changes which machine is called, that quota and permission still answer
to the alias the member asked for, and that a member who asked for `coding` is
never told about the model that answered.
"""

from __future__ import annotations

import httpx
import pytest
import respx
import yaml

MAIN = "http://dgx03:8000"        # coding
WIDE = "http://dgx-wide:8000"     # coding-long
BACKUP = "http://dgx-backup:8000"  # coding-backup

REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "backend-name",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _sibling(document: dict, alias: str, base_url: str, context_tokens: int) -> dict:
    """A copy of `coding` under a new alias, on its own machine."""
    clone = yaml.safe_load(yaml.safe_dump(document))
    clone["metadata"]["alias"] = alias
    clone["spec"]["limits"]["context_tokens"] = context_tokens
    clone["spec"]["endpoints"][0]["name"] = alias
    clone["spec"]["endpoints"][0]["base_url"] = base_url
    clone["spec"].pop("routing", None)
    return clone


@pytest.fixture
def routed_config(writable_config):
    """`coding` (small window) -> overflow to `coding-long`, fallback to `coding-backup`."""
    models = writable_config / "models"
    document = yaml.safe_load((models / "coding.yaml").read_text())

    for alias, url, ctx in (("coding-long", WIDE, 262144), ("coding-backup", BACKUP, 262144)):
        (models / f"{alias}.yaml").write_text(
            yaml.safe_dump(_sibling(document, alias, url, ctx), sort_keys=False, allow_unicode=True)
        )

    document["spec"]["limits"]["context_tokens"] = 2000
    document["spec"]["routing"] = {
        "overflow": "coding-long",
        "fallback": ["coding-backup"],
    }
    (models / "coding.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    )
    return writable_config


def ask(client, key, text: str, **extra):
    return client.post(
        "/v1/chat/completions",
        headers=auth(key),
        json={"model": "coding", "messages": [{"role": "user", "content": text}], **extra},
    )


@respx.mock
def test_an_over_long_prompt_is_served_instead_of_rejected(routed_config, client, member_key):
    """เดิมคำขอนี้ได้ 400 ทั้งที่มีเครื่อง 256K ว่างอยู่"""
    main = respx.post(f"{MAIN}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )
    wide = respx.post(f"{WIDE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    response = ask(client, member_key, "ก" * 200_000)

    assert response.status_code == 200, response.text
    assert wide.called, "prompt ยาวเกิน window ต้องไปเครื่องที่รับไหว"
    assert not main.called


@respx.mock
def test_a_normal_prompt_still_goes_to_the_model_that_was_asked_for(
    routed_config, client, member_key
):
    main = respx.post(f"{MAIN}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )
    wide = respx.post(f"{WIDE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    assert ask(client, member_key, "สวัสดี").status_code == 200
    assert main.called and not wide.called


@respx.mock
def test_when_every_machine_of_the_alias_is_down_the_backup_model_answers(
    routed_config, client, member_key
):
    """endpoint failover จบเมื่อเครื่องของ alias หมด — ตรงนี้คือชั้นถัดไป"""
    down = respx.post(f"{MAIN}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    backup = respx.post(f"{BACKUP}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    response = ask(client, member_key, "สวัสดี")

    assert response.status_code == 200, response.text
    assert down.called and backup.called


@respx.mock
def test_the_member_is_never_told_which_model_actually_answered(routed_config, client, member_key):
    """PRD §6: ชื่อ repo และการจัดเส้นทางภายในไม่ใช่เรื่องของสมาชิก"""
    respx.post(f"{WIDE}/v1/chat/completions").mock(return_value=httpx.Response(200, json=REPLY))

    response = ask(client, member_key, "ก" * 200_000)

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "coding", "ต้องตอบด้วย alias ที่ผู้ใช้ขอ"
    assert "coding-long" not in response.text


@respx.mock
def test_claude_code_gets_the_same_treatment_on_the_anthropic_surface(
    routed_config, client, member_key
):
    """Claude Code เป็นตัวที่ยิง context ยาวที่สุด — /v1/messages ต้องได้กฎเดียวกัน"""
    main = respx.post(f"{MAIN}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )
    wide = respx.post(f"{WIDE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=REPLY)
    )

    response = client.post(
        "/v1/messages",
        headers=auth(member_key),
        json={
            "model": "coding",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "ก" * 200_000}],
        },
    )

    assert response.status_code == 200, response.text
    assert wide.called and not main.called
    assert response.json()["model"] == "coding", "ต้องตอบด้วย alias ที่ผู้ใช้ขอ"
