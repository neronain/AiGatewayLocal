"""What a backend speaks, measured rather than assumed.

`ProbeResult.protocols` was a field nobody wrote to. `build_advice` read
`protocols.get("anthropic")` from it, so the value was always None and the
advice "this backend speaks OpenAI only" appeared for every backend with tool
calling — including the ones answering /v1/messages perfectly well.

That is how two endpoints came to be registered with `anthropic: false` while
serving Anthropic requests correctly: llama.cpp on dgx-veerasiam at :8000 and
gemma-4 at :8001. A third, bifrost at :8081, genuinely returns 405. Telling
those apart takes a request, not a guess about the server type.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.modeltest import build_advice, probe_backend

BASE = "http://backend:8000"


def _models_route(mock):
    mock.get(f"{BASE}/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "m", "max_model_len": 131072}]})
    )


def _chat_route(mock):
    mock.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "OK"}}]
        })
    )


@pytest.mark.anyio
@respx.mock
async def test_a_backend_that_answers_messages_is_recorded_as_native():
    _models_route(respx.mock)
    _chat_route(respx.mock)
    respx.mock.post(f"{BASE}/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "type": "message", "content": [{"type": "text", "text": "OK"}]
        })
    )
    result = await probe_backend(BASE, "m")
    assert result.protocols["anthropic"] is True
    assert result.protocols["openai"] is True


@pytest.mark.anyio
@respx.mock
async def test_a_backend_that_refuses_messages_is_recorded_as_openai_only():
    """bifrost ตอบ 405 จริง — ต้องแยกออกจากตัวที่ตอบได้"""
    _models_route(respx.mock)
    _chat_route(respx.mock)
    respx.mock.post(f"{BASE}/v1/messages").mock(return_value=httpx.Response(405))
    result = await probe_backend(BASE, "m")
    assert result.protocols["anthropic"] is False
    assert any("405" in note for note in result.notes)


@pytest.mark.anyio
@respx.mock
async def test_a_200_that_is_not_an_anthropic_message_does_not_count():
    """บาง proxy ตอบ 200 พร้อม body รูปอื่น — นับว่าไม่รองรับ"""
    _models_route(respx.mock)
    _chat_route(respx.mock)
    respx.mock.post(f"{BASE}/v1/messages").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    )
    result = await probe_backend(BASE, "m")
    assert result.protocols["anthropic"] is False


# ── คำแนะนำที่ตามมา ─────────────────────────────────────────────────────────

def _result(**kwargs):
    from app.core.modeltest import ProbeResult

    result = ProbeResult(reachable=True, upstream_model="m")
    result.capabilities.update(kwargs.pop("capabilities", {}))
    result.protocols.update(kwargs.pop("protocols", {}))
    return result


def test_a_native_backend_is_told_to_declare_it():
    """เคสจริงที่พลาดมาแล้วสองที่: ตอบได้แต่ registry ประกาศ false"""
    advice = build_advice(_result(capabilities={"tools": True}, protocols={"anthropic": True}))
    assert any(a.issue == "anthropic_native" for a in advice)
    assert not any(a.issue == "anthropic_via_translation" for a in advice)


def test_an_openai_only_backend_is_told_the_gateway_will_translate():
    advice = build_advice(_result(capabilities={"tools": True}, protocols={"anthropic": False}))
    assert any(a.issue == "anthropic_via_translation" for a in advice)
    assert not any(a.issue == "anthropic_native" for a in advice)


def test_an_unprobed_backend_gets_neither_piece_of_advice():
    """ไม่รู้ ต่างจาก รู้ว่าไม่ — เดิมสองอย่างนี้ปนกันจนแนะนำผิดทุกเครื่อง"""
    advice = build_advice(_result(capabilities={"tools": True}))
    assert not any(a.issue in {"anthropic_native", "anthropic_via_translation"} for a in advice)
