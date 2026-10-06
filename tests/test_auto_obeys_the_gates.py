"""`model="auto"` ต้องไม่เลือกโมเดลที่ด่านถัดไปจะปฏิเสธ ในเมื่อมีตัวอื่นรับได้

ตรวจ 2026-10-06 บนทะเบียนตัวอย่าง (coding · gemma-vision · muse-local):

    coding ตั้ง capabilities.tools: false   auto + tools -> 400 MODEL_CAPABILITY_NOT_SUPPORTED
    coding ตั้ง protocols.openai: false     auto         -> 400 PROTOCOL_NOT_SUPPORTED

ทั้งสองเคสมีอีกสองโมเดลที่รับคำขอนั้นได้ · `auto` กรองแค่เครื่องกับขนาด context แล้วปล่อยให้
ด่าน capability/surface ไปตรวจกับตัวที่ชนะตัวเดียว — เทสข้างล่างยิงผ่านทางเดินจริงแล้วดูว่า
**ใครตอบ** ไม่ได้ดูว่าฟังก์ชันไหนถูกเรียก
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

CODING = "http://dgx03:8000"
MUSE = "http://dgx01:8000"
GEMMA = "http://dgx02:8000"

REPLY = {
    "id": "c", "object": "chat.completion", "model": "up",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
STREAM = (b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
          b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
          b"data: [DONE]\n\n")
TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
ASK = [{"role": "user", "content": "read main.py"}]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def edit(config, alias: str, change) -> None:
    path = config / "models" / f"{alias}.yaml"
    document = yaml.safe_load(path.read_text())
    change(document["spec"])
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


@pytest.fixture
def backends():
    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=STREAM,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=REPLY)

    with respx.mock:
        yield {name: respx.post(f"{url}/v1/chat/completions").mock(side_effect=answer)
               for name, url in (("coding", CODING), ("muse-local", MUSE),
                                 ("gemma-vision", GEMMA))}


def _auto(client, key, **body):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": "auto", "messages": ASK, **body})


# ---------------------------------------------------------------------------
# ตัวที่ด่านจะปฏิเสธ ต้องไม่ถูกเลือก — และตัวที่รับได้ต้องได้ตอบ
# ---------------------------------------------------------------------------
CASES = {
    "no-tools": (lambda spec: spec["capabilities"].__setitem__("tools", False),
                 {"tools": TOOLS}),
    "closed-on-chat": (lambda spec: spec["protocols"].__setitem__("openai", False), {}),
    "no-streaming": (lambda spec: spec["capabilities"].__setitem__("streaming", False),
                     {"stream": True}),
    "disabled": (lambda spec: spec.__setitem__("enabled", False), {}),
}


@pytest.mark.parametrize("case", CASES)
def test_auto_skips_a_model_the_next_gate_would_refuse(
        writable_config, backends, client, member_key, case):
    change, extra = CASES[case]
    edit(writable_config, "coding", change)
    client.app.state.services.registry.reload()

    response = _auto(client, member_key, **extra)
    response.read()

    assert response.status_code == 200, response.text
    served = response.headers["x-litegate-served-by"]
    assert served in {"gemma-vision", "muse-local"}, served
    assert not backends["coding"].called, "coding รับคำขอรูปนี้ไม่ได้ ต้องไม่ถูกส่งไป"
    assert backends[served].called, "ต้องมีโมเดลตอบจริง ไม่ใช่แค่ header บอกชื่อ"


def test_the_same_model_is_still_a_candidate_for_requests_it_can_take(
        writable_config, client, member_key):
    """ตัวควบคุม: ตัดเฉพาะคำขอที่มันรับไม่ได้ ไม่ใช่ตัดโมเดลออกจาก auto ทั้งตัว"""
    from app.core import auto
    from app.core.multimodal import profile_openai_request
    from app.registry.schema import VisionPolicy

    edit(writable_config, "coding", lambda spec: spec["capabilities"].__setitem__("tools", False))
    registry = client.app.state.services.registry
    registry.reload()
    models = list(registry.snapshot.models.values())

    def aliases(body: dict) -> set[str]:
        profile = profile_openai_request({"messages": ASK, **body}, VisionPolicy())
        return {m.alias for m in auto.candidates(models, profile=profile, protocol="openai")}

    assert "coding" in aliases({})
    assert "coding" not in aliases({"tools": TOOLS})


def test_every_candidate_passes_the_gates_the_request_path_runs_next(client):
    """กติกาที่ต้องจริงเสมอ ไม่ว่าทะเบียนจะหน้าตาแบบไหน: ผู้สมัครทุกตัวผ่านด่านจริง"""
    from app.core import auto
    from app.core.capability import validate_model_capabilities, validate_protocol
    from app.core.multimodal import profile_openai_request
    from app.registry.schema import VisionPolicy
    from tests.conftest import png_data_url

    models = list(client.app.state.services.registry.snapshot.models.values())
    image = {"type": "image_url", "image_url": {"url": png_data_url(8, 8)}}
    shapes = [
        {"messages": ASK},
        {"messages": ASK, "tools": TOOLS},
        {"messages": ASK, "stream": True},
        {"messages": [{"role": "user", "content": [image]}]},
        {"messages": [{"role": "user", "content": [image]}], "tools": TOOLS, "stream": True},
    ]
    for body in shapes:
        profile = profile_openai_request(body, VisionPolicy())
        for surface in ("openai", "anthropic", "responses"):
            for model in auto.candidates(models, profile=profile, protocol=surface):
                validate_protocol(model, surface)              # โยน = เทสล้ม
                validate_model_capabilities(model, profile)


# ---------------------------------------------------------------------------
# ไม่มีตัวไหนรับได้ — บอกว่าทำไม และไม่ข้ามสิทธิ์
# ---------------------------------------------------------------------------
def test_when_nothing_qualifies_the_error_says_what_the_request_needs(
        writable_config, backends, client, member_key):
    for alias in ("coding", "gemma-vision", "muse-local"):
        edit(writable_config, alias, lambda spec: spec["capabilities"].update(
            {"tools": False, "agentic": False}))
    client.app.state.services.registry.reload()

    response = _auto(client, member_key, tools=TOOLS)

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "MODEL_NOT_FOUND"
    assert error["param"] == "model"
    assert "tools" in error["message"], error["message"]
    assert error["details"]["required_capabilities"] == ["tools"]
    assert set(error["details"]["available_models"]) == {"coding", "gemma-vision", "muse-local"}
    assert not any(route.called for route in backends.values())


def test_auto_does_not_reach_past_the_keys_own_models(
        writable_config, backends, client):
    """coding รับ tools ไม่ได้ และ key ใบนี้ใช้ได้แค่ coding — ต้องจบที่ 404 ไม่ใช่ไปตัวอื่น"""
    edit(writable_config, "coding", lambda spec: spec["capabilities"].update(
        {"tools": False, "agentic": False}))
    client.app.state.services.registry.reload()
    admin = auth(client.admin_key)
    person = client.post("/admin/users", headers=admin, json={"external_id": "dev2"}).json()
    key = client.post("/admin/api-keys", headers=admin, json={
        "user_id": person["id"], "name": "k", "models": ["coding"]}).json()["api_key"]

    response = _auto(client, key, tools=TOOLS)

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["details"]["available_models"] == ["coding"]
    for hidden in ("gemma-vision", "muse-local"):
        assert hidden not in error["message"]
    assert not any(route.called for route in backends.values())
