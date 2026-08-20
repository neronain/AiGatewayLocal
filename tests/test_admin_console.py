"""Registry authoring, capability probing and console-triggered test runs."""

from __future__ import annotations

import httpx
import respx
import yaml

UPSTREAM = "http://newbox:9000"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def definition(alias: str = "newmodel", **overrides) -> dict:
    spec = {
        "upstream_model": "org/Some-Model",
        "purpose": ["general"],
        "limits": {"context_tokens": 32768, "max_output_tokens": 2048},
        "modalities": {"input": ["text"], "output": ["text"]},
        "capabilities": {"chat": True, "streaming": True, "tools": True},
        "protocols": {"openai": True, "anthropic": False},
        "endpoints": [
            {
                "name": "box1",
                "server_type": "vllm",
                "base_url": UPSTREAM,
                "protocols": {"openai": True, "anthropic": False},
                "modalities": {"text": True, "image": False},
            }
        ],
    }
    spec.update(overrides)
    return {
        "apiVersion": "litegate.dev/v1",
        "kind": "Model",
        "metadata": {"alias": alias, "display_name": "New Model", "visibility": "member"},
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# Preview (mode A)
# ---------------------------------------------------------------------------
def test_preview_renders_valid_yaml_without_touching_disk(client):
    response = client.post(
        "/admin/models/preview", json=definition(), headers=auth(client.admin_key)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "newmodel.yaml"

    parsed = yaml.safe_load(body["yaml"])
    assert parsed["metadata"]["alias"] == "newmodel"
    assert parsed["spec"]["upstream_model"] == "org/Some-Model"
    # Preview must not register anything.
    listed = client.get("/admin/models", headers=auth(client.admin_key)).json()
    assert "newmodel" not in {m["alias"] for m in listed["data"]}


def test_preview_rejects_contradictory_capabilities(client):
    """vision=true with a text-only endpoint must fail before it can be saved."""
    bad = definition(
        capabilities={"chat": True, "streaming": True, "vision": True},
        modalities={"input": ["text", "image"], "output": ["text"]},
    )
    response = client.post("/admin/models/preview", json=bad, headers=auth(client.admin_key))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert "problems" in error["details"]


def test_preview_rejects_bad_alias(client):
    response = client.post(
        "/admin/models/preview", json=definition("../escape"), headers=auth(client.admin_key)
    )
    assert response.status_code == 400


def test_model_authoring_is_admin_only(client, member_key):
    assert client.post(
        "/admin/models/preview", json=definition(), headers=auth(member_key)
    ).status_code == 403
    assert client.post(
        "/admin/models", json=definition(), headers=auth(member_key)
    ).status_code == 403


# ---------------------------------------------------------------------------
# Save / delete (mode B)
# ---------------------------------------------------------------------------
def test_save_writes_file_and_registers_model(writable_config, client):
    response = client.post(
        "/admin/models", json=definition(), headers=auth(client.admin_key)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True

    written = writable_config / "models" / "newmodel.yaml"
    assert written.exists()
    assert yaml.safe_load(written.read_text())["metadata"]["alias"] == "newmodel"

    listed = client.get("/admin/models", headers=auth(client.admin_key)).json()
    assert "newmodel" in {m["alias"] for m in listed["data"]}

    # And it is immediately usable through the public catalogue.
    models = client.get("/v1/models", headers=auth(client.admin_key)).json()
    assert "newmodel" in {m["id"] for m in models["data"]}


def test_save_twice_updates_rather_than_duplicates(writable_config, client):
    client.post("/admin/models", json=definition(), headers=auth(client.admin_key))
    updated = definition()
    updated["metadata"]["display_name"] = "Renamed"
    response = client.post("/admin/models", json=updated, headers=auth(client.admin_key))
    assert response.status_code == 201
    assert response.json()["created"] is False

    listed = client.get("/admin/models", headers=auth(client.admin_key)).json()
    entries = [m for m in listed["data"] if m["alias"] == "newmodel"]
    assert len(entries) == 1
    assert entries[0]["display_name"] == "Renamed"


def test_delete_removes_file_and_deregisters(writable_config, client):
    client.post("/admin/models", json=definition(), headers=auth(client.admin_key))
    response = client.delete("/admin/models/newmodel", headers=auth(client.admin_key))
    assert response.status_code == 200
    assert not (writable_config / "models" / "newmodel.yaml").exists()

    listed = client.get("/admin/models", headers=auth(client.admin_key)).json()
    assert "newmodel" not in {m["alias"] for m in listed["data"]}


def test_delete_unknown_model_is_404(writable_config, client):
    response = client.delete("/admin/models/ghost", headers=auth(client.admin_key))
    assert response.status_code == 404


def test_registry_status_reports_writability(writable_config, client):
    body = client.get("/admin/registry/status", headers=auth(client.admin_key)).json()
    assert body["writable"] is True
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------
@respx.mock
def test_detect_reports_measured_capabilities(client):
    """A vision-named model served without a projector must come back vision=false."""
    respx.get(f"{UPSTREAM}/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "org/gemma-vision-31B", "max_model_len": 65536}]},
        )
    )

    def chat_router(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        content = payload["messages"][-1].get("content")
        has_image = isinstance(content, list) and any(
            b.get("type") == "image_url" for b in content
        )
        if has_image:
            return httpx.Response(
                500, json={"error": {"message": "image input is not supported"}}
            )
        if payload.get("tools"):
            # Accepts tools but never emits tool_calls - the vLLM parser case.
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "sure"}}]},
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )

    respx.post(f"{UPSTREAM}/v1/chat/completions").mock(side_effect=chat_router)
    respx.post(f"{UPSTREAM}/v1/messages").mock(return_value=httpx.Response(404))

    response = client.post(
        "/admin/models/detect", json={"base_url": UPSTREAM}, headers=auth(client.admin_key)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["confirmed"] is False  # admin must confirm (FR-39)

    suggestion = body["suggestion"]
    assert suggestion["reachable"] is True
    assert suggestion["upstream_model"] == "org/gemma-vision-31B"
    assert suggestion["context_tokens"] == 65536
    assert suggestion["capabilities"]["chat"] is True
    assert suggestion["capabilities"]["vision"] is False
    assert suggestion["capabilities"]["tools"] is False
    assert any("tool_calls" in n for n in suggestion["notes"])


@respx.mock
def test_detect_unreachable_backend_is_reported_not_raised(client):
    respx.get(f"{UPSTREAM}/v1/models").mock(side_effect=httpx.ConnectError("refused"))
    response = client.post(
        "/admin/models/detect", json={"base_url": UPSTREAM}, headers=auth(client.admin_key)
    )
    assert response.status_code == 200
    assert response.json()["suggestion"]["reachable"] is False


# ---------------------------------------------------------------------------
# Console-triggered test runs
# ---------------------------------------------------------------------------
def test_test_run_is_created_and_pollable(client):
    response = client.post(
        "/admin/models/coding/test?only=MODEL-001", headers=auth(client.admin_key)
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    run = client.get(f"/admin/test-runs/{run_id}", headers=auth(client.admin_key)).json()
    assert run["model"] == "coding"
    assert run["total"] == 1
    assert run["status"] in {"running", "done", "error"}


def test_test_run_on_unknown_model_is_404(client):
    response = client.post("/admin/models/ghost/test", headers=auth(client.admin_key))
    assert response.status_code == 404


def test_test_run_is_admin_only(client, member_key):
    response = client.post("/admin/models/coding/test", headers=auth(member_key))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Per-endpoint upstream override (failover across heterogeneous backends)
# ---------------------------------------------------------------------------
def test_endpoint_upstream_override_is_accepted(client):
    """Two backends serving the same weights under different names."""
    spec = definition()["spec"]
    spec["endpoints"] = [
        {
            "name": "direct",
            "server_type": "llama.cpp",
            "base_url": "http://direct:8000",
            "priority": 100,
            "protocols": {"openai": True, "anthropic": False},
            "modalities": {"text": True, "image": False},
        },
        {
            "name": "router",
            "server_type": "openai_compatible",
            "base_url": "http://router:8081",
            "upstream_model": "Ai1/org-Some-Model",
            "priority": 50,
            "protocols": {"openai": True, "anthropic": False},
            "modalities": {"text": True, "image": False},
        },
    ]
    payload = definition()
    payload["spec"] = spec

    response = client.post(
        "/admin/models/preview", json=payload, headers=auth(client.admin_key)
    )
    assert response.status_code == 200
    parsed = yaml.safe_load(response.json()["yaml"])
    endpoints = parsed["spec"]["endpoints"]
    assert endpoints[0].get("upstream_model", "") == ""
    assert endpoints[1]["upstream_model"] == "Ai1/org-Some-Model"


@respx.mock
def test_request_uses_the_endpoints_own_model_name(client, member_key):
    """The name sent upstream must be the one that backend knows."""
    from app.registry.schema import Endpoint
    from tests.conftest import REPO_ROOT  # noqa: F401

    reply = {
        "id": "c1",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }
    route = respx.post("http://router:8081/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=reply)
    )

    # Point the live 'coding' model at a router that renames the model.
    snapshot = client.app.state.services.registry.snapshot
    model = snapshot.models["coding"]
    model.spec.endpoints[:] = [
        Endpoint(
            name="router",
            server_type="openai_compatible",
            base_url="http://router:8081",
            upstream_model="Ai1/Qwen3-Coder-30B-A3B-Instruct",
            protocols={"openai": True, "anthropic": False},
            modalities={"text": True, "image": False},
        )
    ]

    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    sent = __import__("json").loads(route.calls[0].request.content)
    assert sent["model"] == "Ai1/Qwen3-Coder-30B-A3B-Instruct"
    # The member still only ever sees the alias.
    assert response.json()["model"] == "coding"


# ---------------------------------------------------------------------------
# Verify & advise
# ---------------------------------------------------------------------------
@respx.mock
def test_advice_names_the_missing_vllm_flag(client):
    """A tools rejection must come back with the flag and the command to run."""
    base = "http://dgx03:8000"  # coding, per config/models/coding.yaml
    respx.get(f"{base}/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "Qwen3-Coder-30B-A3B-Instruct", "max_model_len": 65536}]}
        )
    )

    def router(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        if payload.get("tools"):
            return httpx.Response(
                400,
                json={"error": {"message": '"auto" tool choice requires '
                                           "--enable-auto-tool-choice and "
                                           "--tool-call-parser to be set"}},
            )
        content = payload["messages"][-1].get("content")
        if isinstance(content, list):
            return httpx.Response(400, json={"error": {"message": "not a multimodal model"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )

    respx.post(f"{base}/v1/chat/completions").mock(side_effect=router)
    respx.post(f"{base}/v1/messages").mock(return_value=httpx.Response(404))

    body = client.get(
        "/admin/models/coding/advice", headers=auth(client.admin_key)
    ).json()

    assert body["model"] == "coding"
    backend = body["backends"][0]
    assert backend["reachable"] is True
    assert backend["measured"]["tools"] is False

    issues = {a["issue"]: a for a in backend["advice"]}
    assert "tools_flag_missing" in issues
    fix = issues["tools_flag_missing"]
    assert "--enable-auto-tool-choice" in fix["detail"]
    # The parser must be the one built for this family, not a generic guess.
    assert "--tool-parser qwen3_coder" in fix["command"]

    # The registry says tools=true; the backend just proved otherwise.
    assert {"capability": "tools", "declared": True, "measured": False} in backend["drift"]
    assert body["summary"]["verdict"] == "drift"


@respx.mock
def test_advice_reports_a_missing_projector(client):
    base = "http://dgx02:8000"  # gemma-vision, per config/models/gemma-vision.yaml
    respx.get(f"{base}/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "some/vision-model"}]})
    )

    def router(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        content = payload["messages"][-1].get("content")
        if isinstance(content, list):
            return httpx.Response(
                500,
                json={"error": {"message": "image input is not supported - hint: if this "
                                           "is unexpected, you may need to provide the mmproj"}},
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )

    respx.post(f"{base}/v1/chat/completions").mock(side_effect=router)
    respx.post(f"{base}/v1/messages").mock(return_value=httpx.Response(404))

    body = client.get(
        "/admin/models/gemma-vision/advice", headers=auth(client.admin_key)
    ).json()
    issues = {a["issue"] for a in body["backends"][0]["advice"]}
    assert "projector_missing" in issues


def test_advice_is_admin_only(client, member_key):
    assert client.get(
        "/admin/models/coding/advice", headers=auth(member_key)
    ).status_code == 403


def test_advice_on_unknown_model_is_404(client):
    assert client.get(
        "/admin/models/ghost/advice", headers=auth(client.admin_key)
    ).status_code == 404


# ---------------------------------------------------------------------------
# Rename compatibility
# ---------------------------------------------------------------------------
def test_registry_written_before_the_rename_still_loads(client):
    """A model file with the old apiVersion must not need editing."""
    legacy = definition()
    legacy["apiVersion"] = "edullm.gateway/v1"
    response = client.post(
        "/admin/models/preview", json=legacy, headers=auth(client.admin_key)
    )
    assert response.status_code == 200


def test_keys_issued_before_the_rename_still_authenticate(client):
    """Verification is HMAC over the whole key, so the prefix is only a label."""
    import asyncio

    from app.core.auth import hash_api_key
    from app.db.models import ApiKey, User
    from app.db.session import session_scope

    legacy_key = "edu_sk_" + "L" * 32

    async def seed() -> None:
        async with session_scope() as session:
            user = User(external_id="legacy-user", display_name="Legacy", role="member")
            session.add(user)
            await session.flush()
            session.add(
                ApiKey(
                    user_id=user.id,
                    name="issued before the rename",
                    key_prefix=legacy_key[:12],
                    key_hash=hash_api_key(legacy_key),
                    scopes=[],
                )
            )

    asyncio.run(seed())

    response = client.get("/v1/me", headers=auth(legacy_key))
    assert response.status_code == 200
    assert response.json()["external_id"] == "legacy-user"


def test_new_keys_use_the_new_prefix(client):
    users = client.get("/admin/users", headers=auth(client.admin_key)).json()["data"]
    target = users[0]["id"]
    issued = client.post(
        "/admin/api-keys", json={"user_id": target}, headers=auth(client.admin_key)
    ).json()
    assert issued["api_key"].startswith("lg_sk_")


@respx.mock
def test_responses_carry_both_header_names_for_one_release(client, member_key):
    respx.post("http://dgx03:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "c1",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )
    )
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.headers["x-litegate-model"] == "coding"
    assert response.headers["x-edullm-model"] == "coding"


def test_registry_with_pre_rename_vocabulary_still_loads(client):
    """`visibility: student` must not empty the registry on upgrade."""
    from app.registry.schema import ModelDefinition, Visibility

    legacy = definition()
    legacy["metadata"]["visibility"] = "student"
    parsed = ModelDefinition.model_validate(legacy)
    assert parsed.metadata.visibility is Visibility.MEMBER

    response = client.post(
        "/admin/models/preview", json=legacy, headers=auth(client.admin_key)
    )
    assert response.status_code == 200


def test_a_manager_stored_as_instructor_keeps_their_rights(client):
    """Roles in the database are not rewritten on upgrade."""
    import asyncio

    from app.core.auth import generate_api_key
    from app.db.models import ApiKey, User
    from app.db.session import session_scope

    async def seed() -> str:
        plaintext, prefix, digest = generate_api_key()
        async with session_scope() as session:
            user = User(external_id="old-staff", display_name="Ajarn", role="instructor")
            session.add(user)
            await session.flush()
            session.add(
                ApiKey(user_id=user.id, name="legacy", key_prefix=prefix,
                       key_hash=digest, scopes=[])
            )
        return plaintext

    key = asyncio.run(seed())
    me = client.get("/v1/me", headers=auth(key)).json()
    assert me["role"] == "manager"
    # And the manager-only surface is reachable.
    assert client.get("/admin/users", headers=auth(key)).status_code == 200


# ---------------------------------------------------------------------------
# Integration with the deploy tool: advice you can paste
# ---------------------------------------------------------------------------
def test_advice_command_names_the_real_controller_when_known():
    """`./<controller>.sh` is advice someone has to translate; a real path is not."""
    from app.core.modeltest import Advice, resolve_commands
    from app.registry.schema import ManagedBy

    finding = Advice(
        issue="tools_flag_missing",
        severity="warning",
        detail="vLLM rejected the tool request.",
        fix="Restart with the parser.",
        command="./<controller>.sh restart --tool-parser qwen3_coder",
    )

    managed = ManagedBy(
        tool="lmds",
        node="neronain@100.80.132.102",
        controller="~/bundles/qwen3-coder/qwen3-coder-single.sh",
    )
    resolved = resolve_commands([finding], managed)[0]
    assert resolved.command == (
        "ssh neronain@100.80.132.102 "
        "'~/bundles/qwen3-coder/qwen3-coder-single.sh restart --tool-parser qwen3_coder'"
    )

    # Local controller, no ssh hop.
    local = ManagedBy(controller="/opt/models/run.sh")
    assert resolve_commands([finding], local)[0].command == (
        "/opt/models/run.sh restart --tool-parser qwen3_coder"
    )


def test_advice_is_unchanged_when_the_deploy_tool_is_unknown():
    """LiteGate must be useful with no deploy tool in the picture at all."""
    from app.core.modeltest import Advice, resolve_commands

    finding = Advice(
        issue="tools_flag_missing", severity="warning", detail="", fix="",
        command="./<controller>.sh restart --tool-parser hermes",
    )
    assert resolve_commands([finding], None)[0].command == finding.command


def test_managed_by_is_optional_in_the_registry(client):
    """A model file that says nothing about deployment still validates."""
    with_tool = definition()
    with_tool["spec"]["endpoints"][0]["managed_by"] = {
        "tool": "lmds",
        "node": "ops@10.0.0.5",
        "controller": "~/bundles/x/x-single.sh",
    }
    assert client.post(
        "/admin/models/preview", json=with_tool, headers=auth(client.admin_key)
    ).status_code == 200
    # And without it.
    assert client.post(
        "/admin/models/preview", json=definition(), headers=auth(client.admin_key)
    ).status_code == 200


@respx.mock
def test_the_header_names_who_actually_answered(client, member_key):
    """ขอ alias ไหนได้ alias นั้น — แต่ต้องรู้ด้วยว่าเบื้องหลังใครตอบ

    กฎ routing เปลี่ยนเส้นทางได้ (coding -> coding-long เมื่อคำขอยาวเกิน) โดยเจตนา
    ไม่เปลี่ยนชื่อที่ echo กลับไป · ก่อนหน้านี้จึงไม่มีทางรู้เลยว่าโมเดลไหนตอบจริง
    ซึ่งทำให้ตัวเลขเร็ว/ช้าที่วัดได้ถูกโยงไปผิดตัว
    """
    respx.post("http://dgx03:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "c1",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )
    )
    response = client.post(
        "/v1/chat/completions",
        headers=auth(member_key),
        json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    # ไม่ได้ reroute → สองตัวนี้ตรงกัน · reroute เมื่อไรถึงจะต่าง
    assert response.headers["x-litegate-model"] == "coding"
    assert response.headers["x-litegate-served-by"] == "coding"
    # ลายเซ็นติดมากับทุก response ผ่าน middleware
    assert response.headers["x-litegate-by"] == "neronain"
