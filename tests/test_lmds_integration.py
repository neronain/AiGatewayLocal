"""Sending a finding back to the tool that can fix it (FR-55).

The interesting cases are the refusals. Applying a fix restarts a model server
on someone's GPU machine, so what matters is that it goes to the right one, or
does not go at all.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core import lmds
from app.core.errors import GatewayError
from app.registry.schema import ManagedBy

LMDS = "http://lmds.local:8600"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _managed(**overrides) -> ManagedBy:
    return ManagedBy(**{
        "tool": "lmds", "node": "ops@10.0.0.6", "controller": "~/b/x.sh",
        "lmds_node": "msi-6", "lmds_slug": "coder-next", **overrides,
    })


def _connection(token: str = "t") -> lmds.Connection:
    return lmds.Connection(base_url=LMDS, token=token)


# ---------------------------------------------------------------------------
# Optional by construction
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_deploy_tool_means_no_remote_action():
    """A gateway that was never told about a deploy tool must not invent one."""
    with pytest.raises(GatewayError, match="No deploy tool is connected"):
        await lmds.apply_fix(lmds.Connection("", ""), _managed(), "tools_flag_missing", "hermes")


@pytest.mark.asyncio
async def test_an_endpoint_nobody_manages_is_refused():
    """One LMDS-managed backend does not make every backend LMDS-managed."""
    with pytest.raises(GatewayError, match="does not say which LMDS machine"):
        await lmds.apply_fix(
            _connection(), _managed(lmds_node="", lmds_slug=""), "tools_flag_missing", "hermes"
        )


@pytest.mark.asyncio
async def test_another_tool_is_not_sent_to_lmds():
    with pytest.raises(GatewayError, match="managed by 'ansible'"):
        await lmds.apply_fix(_connection(), _managed(tool="ansible"), "tools_flag_missing", "x")


@pytest.mark.asyncio
async def test_only_findings_on_the_list_can_be_applied():
    """This is not a remote shell with a friendly name."""
    with pytest.raises(GatewayError, match="not a finding that can be applied"):
        await lmds.apply_fix(_connection(), _managed(), "projector_missing", "hermes")


# ---------------------------------------------------------------------------
# What actually goes over the wire
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.asyncio
async def test_the_request_names_the_lmds_node_and_bundle_not_the_ssh_target():
    """LMDS addresses machines by registry name; the ssh target is for humans."""
    route = respx.post(f"{LMDS}/api/nodes/msi-6/models/coder-next/restart").mock(
        return_value=httpx.Response(200, json={"job": {"id": "j1"}})
    )

    result = await lmds.apply_fix(_connection(), _managed(), "tools_flag_missing", "qwen3_coder")

    assert route.called
    sent = route.calls[0].request
    assert b'"tool_parser"' in sent.content
    assert b"qwen3_coder" in sent.content
    assert sent.headers["x-lmds-token"] == "t"
    assert result["applied"] == {"tool_parser": "qwen3_coder"}
    assert result["job"] == {"id": "j1"}


@respx.mock
@pytest.mark.asyncio
async def test_the_reasoning_finding_sets_the_reasoning_parser():
    route = respx.post(f"{LMDS}/api/nodes/msi-6/models/coder-next/restart").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await lmds.apply_fix(
        _connection(), _managed(), "reasoning_not_separated", "deepseek_r1"
    )
    assert b'"reasoning_parser"' in route.calls[0].request.content
    assert result["applied"] == {"reasoning_parser": "deepseek_r1"}


@respx.mock
@pytest.mark.asyncio
async def test_a_refusal_from_the_deploy_tool_is_passed_through_not_reworded():
    """LMDS answers in the operator's language and knows why it said no."""
    respx.post(f"{LMDS}/api/nodes/msi-6/models/coder-next/restart").mock(
        return_value=httpx.Response(400, json={"detail": "ไม่รู้จักเครื่อง msi-6"})
    )

    with pytest.raises(GatewayError, match="ไม่รู้จักเครื่อง"):
        await lmds.apply_fix(_connection(), _managed(), "tools_flag_missing", "hermes")


@respx.mock
@pytest.mark.asyncio
async def test_an_unreachable_deploy_tool_is_reported_as_such():
    respx.post(f"{LMDS}/api/nodes/msi-6/models/coder-next/restart").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(GatewayError, match="Could not reach the deploy tool"):
        await lmds.apply_fix(_connection(), _managed(), "tools_flag_missing", "hermes")


# ---------------------------------------------------------------------------
# Parser suggestion
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("served", "parser"),
    [
        ("deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", "deepseek_r1"),
        ("Qwen/Qwen3-32B", "deepseek_r1"),
        ("ibm-granite/granite-3.2-8b", "granite"),
    ],
)
def test_a_known_family_gets_a_confident_reasoning_parser(served, parser):
    assert lmds.suggest_reasoning_parser(served) == (parser, True)


def test_an_unknown_family_is_marked_as_a_guess():
    """A wrong reasoning parser does not error, it silently separates nothing."""
    _, confident = lmds.suggest_reasoning_parser("some-org/private-model-v2")
    assert confident is False


# ---------------------------------------------------------------------------
# The admin plane
# ---------------------------------------------------------------------------
def test_the_token_is_never_returned_to_the_console(client):
    client.put(
        "/admin/integrations/lmds",
        headers=auth(client.admin_key),
        json={"base_url": LMDS, "token": "super-secret"},
    )

    body = client.get("/admin/integrations/lmds", headers=auth(client.admin_key)).json()
    assert body["has_token"] is True
    assert "super-secret" not in str(body)


def test_a_base_url_without_a_scheme_is_refused(client):
    response = client.put(
        "/admin/integrations/lmds", headers=auth(client.admin_key), json={"base_url": "lmds:8600"}
    )
    assert response.status_code == 400


def test_omitting_the_token_keeps_the_stored_one(client):
    """Editing the URL should not silently disconnect the tool."""
    client.put(
        "/admin/integrations/lmds",
        headers=auth(client.admin_key),
        json={"base_url": LMDS, "token": "keep-me"},
    )
    body = client.put(
        "/admin/integrations/lmds",
        headers=auth(client.admin_key),
        json={"base_url": LMDS + "/"},
    ).json()
    assert body["has_token"] is True


def test_a_member_cannot_connect_a_deploy_tool(client, member_key):
    assert client.get("/admin/integrations/lmds", headers=auth(member_key)).status_code == 403
    assert client.put(
        "/admin/integrations/lmds", headers=auth(member_key), json={"base_url": LMDS}
    ).status_code == 403


def test_a_parser_name_with_shell_characters_is_refused(client):
    """The name ends up in a command on another machine."""
    client.put(
        "/admin/integrations/lmds", headers=auth(client.admin_key), json={"base_url": LMDS}
    )
    response = client.post(
        "/admin/models/coding/apply-fix",
        headers=auth(client.admin_key),
        json={"issue": "tools_flag_missing", "parser": "hermes; rm -rf /"},
    )
    assert response.status_code == 400
    assert "letters, digits" in response.json()["error"]["message"]


def test_applying_to_an_unmanaged_model_says_to_run_it_yourself(client):
    client.put(
        "/admin/integrations/lmds", headers=auth(client.admin_key), json={"base_url": LMDS}
    )
    response = client.post(
        "/admin/models/coding/apply-fix",
        headers=auth(client.admin_key),
        json={"issue": "tools_flag_missing", "parser": "hermes"},
    )
    assert response.status_code == 400
    assert "Run the command yourself" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Knowing which fleet you connected to
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.asyncio
async def test_a_successful_check_names_the_fleet():
    """"Connected" is not the question - "connected to which one" is."""
    respx.get(f"{LMDS}/api/host").mock(
        return_value=httpx.Response(
            200, json={"hostname": "Autodeploy", "ip": "10.0.0.2", "lmds_version": "0.2.0"}
        )
    )
    respx.get(f"{LMDS}/api/nodes").mock(
        return_value=httpx.Response(200, json={"nodes": [{"name": "msi-5"}, {"name": "msi-6"}]})
    )

    result = await lmds.check(_connection())
    assert result["ok"] is True
    assert result["hostname"] == "Autodeploy"
    assert result["nodes"] == 2
    assert result["node_names"] == ["msi-5", "msi-6"]


@respx.mock
@pytest.mark.asyncio
async def test_a_rejected_token_is_named_as_such():
    """The most common mistake: right URL, wrong token. Say so precisely."""
    respx.get(f"{LMDS}/api/host").mock(return_value=httpx.Response(401, json={"detail": "no"}))

    result = await lmds.check(_connection("stale"))
    assert result["ok"] is False
    assert "rejected the token" in result["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_an_unreachable_tool_is_not_reported_as_connected():
    respx.get(f"{LMDS}/api/host").mock(side_effect=httpx.ConnectError("refused"))

    result = await lmds.check(_connection())
    assert result["ok"] is False
    assert "Could not reach" in result["reason"]


@pytest.mark.asyncio
async def test_checking_with_nothing_configured_says_so():
    assert (await lmds.check(lmds.Connection("", "")))["ok"] is False


def test_only_an_admin_can_test_the_connection(client, member_key):
    assert client.post(
        "/admin/integrations/lmds/test", headers=auth(member_key)
    ).status_code == 403


# ---------------------------------------------------------------------------
# Surviving an edit in the console (WS-5.1)
#
# `managed_by` is the only thing that says which LMDS node and bundle a backend
# came from, and nothing in the request path reads it - so losing it breaks
# nothing visibly. The Apply-fix button just stops appearing, months after the
# edit that removed it, and the model file on disk is the only evidence.
#
# The console holds no copy of a model: it fills the editor from
# `GET /admin/models` and posts the form back as a whole document. A field the
# GET leaves out is therefore a field the next Save deletes.
# ---------------------------------------------------------------------------
MANAGED = {
    "tool": "lmds",
    "node": "neronain@100.80.132.102",
    "controller": "~/bundles/coder-next/controller.sh",
    "lmds_node": "msi-6",
    "lmds_slug": "coder-next",
}


def _definition(alias: str = "managed-model", *, managed: dict | None = MANAGED) -> dict:
    endpoint = {
        "name": "box1",
        "server_type": "vllm",
        "base_url": "http://newbox:9000",
        "protocols": {"openai": True, "anthropic": False},
        "modalities": {"text": True, "image": False},
    }
    if managed is not None:
        endpoint["managed_by"] = managed
    return {
        "apiVersion": "litegate.dev/v1",
        "kind": "Model",
        "metadata": {"alias": alias, "display_name": "Managed", "visibility": "member"},
        "spec": {
            "upstream_model": "org/Coder-Next",
            "purpose": ["general"],
            "limits": {"context_tokens": 32768, "max_output_tokens": 2048},
            "modalities": {"input": ["text"], "output": ["text"]},
            "capabilities": {"chat": True, "streaming": True, "tools": True},
            "protocols": {"openai": True, "anthropic": False},
            "endpoints": [endpoint],
        },
    }


def _entry(client, alias: str = "managed-model") -> dict:
    listed = client.get("/admin/models", headers=auth(client.admin_key)).json()["data"]
    return next(m for m in listed if m["alias"] == alias)


def _as_the_console_would_save(entry: dict) -> dict:
    """Rebuild the save document out of the GET response, the way app.js does.

    `editorValues()` assembles the POST body from what `openEditor()` was given,
    which is one entry of `GET /admin/models`. `health` is computed per request
    rather than stored, so it is the one key the console drops on purpose -
    everything else it carries through.
    """
    routing = {
        k: v for k, v in (entry.get("routing") or {}).items()
        if v not in (None, [], {})
    }
    spec = {
        "upstream_model": entry["upstream_model"],
        "purpose": entry["purpose"],
        "limits": entry["limits"],
        "modalities": entry["modalities"],
        "capabilities": entry["capabilities"],
        "protocols": entry["protocols"],
        "endpoints": [
            {k: v for k, v in e.items() if k != "health"} for e in entry["endpoints"]
        ],
        "enabled": entry["enabled"],
    }
    if routing:
        spec["routing"] = routing
    return {
        "apiVersion": "litegate.dev/v1",
        "kind": "Model",
        "metadata": {
            "alias": entry["alias"],
            "display_name": entry["display_name"],
            "description": entry["description"],
            "visibility": entry["visibility"],
            "tags": entry["tags"],
        },
        "spec": spec,
    }


def test_the_registry_view_tells_the_console_which_tool_deployed_a_backend(
    writable_config, client
):
    """A field the console is never shown is a field it cannot send back."""
    client.post("/admin/models", json=_definition(), headers=auth(client.admin_key))

    endpoint = _entry(client)["endpoints"][0]
    assert endpoint["managed_by"] == MANAGED


def test_editing_a_model_in_the_console_does_not_unmanage_it(writable_config, client):
    """The whole bug: read the model, save it back unchanged, lose the deploy link."""
    client.post("/admin/models", json=_definition(), headers=auth(client.admin_key))

    document = _as_the_console_would_save(_entry(client))
    document["metadata"]["display_name"] = "Renamed from the console"
    response = client.post("/admin/models", json=document, headers=auth(client.admin_key))
    assert response.status_code == 201

    after = _entry(client)
    assert after["display_name"] == "Renamed from the console"
    # Every field, not just "something is there": `node` and `lmds_node` are
    # different strings on purpose and a half-kept block routes a restart to
    # the wrong machine.
    assert after["endpoints"][0].get("managed_by") == MANAGED


def test_the_file_on_disk_still_says_where_the_backend_came_from(writable_config, client):
    """The registry file is the record; a reload must not resurrect a lost block."""
    import yaml

    client.post("/admin/models", json=_definition(), headers=auth(client.admin_key))
    client.post(
        "/admin/models",
        json=_as_the_console_would_save(_entry(client)),
        headers=auth(client.admin_key),
    )

    written = yaml.safe_load((writable_config / "models" / "managed-model.yaml").read_text())
    assert written["spec"]["endpoints"][0].get("managed_by") == MANAGED


@respx.mock
def test_apply_fix_still_reaches_the_node_after_a_console_edit(writable_config, client):
    """What the erasure actually costs: the button stops working, silently."""
    client.put(
        "/admin/integrations/lmds", headers=auth(client.admin_key), json={"base_url": LMDS}
    )
    client.post("/admin/models", json=_definition(), headers=auth(client.admin_key))
    client.post(
        "/admin/models",
        json=_as_the_console_would_save(_entry(client)),
        headers=auth(client.admin_key),
    )

    route = respx.post(f"{LMDS}/api/nodes/msi-6/models/coder-next/restart").mock(
        return_value=httpx.Response(200, json={"job": {"id": "j1"}})
    )
    response = client.post(
        "/admin/models/managed-model/apply-fix",
        headers=auth(client.admin_key),
        json={"issue": "tools_flag_missing", "parser": "qwen3_coder"},
    )

    assert response.status_code == 200, response.json()
    assert route.called
    assert response.json()["applied"] == {"tool_parser": "qwen3_coder"}


def test_a_member_never_sees_the_ssh_target(client, member_key):
    """`managed_by` names a machine and a script path - admin-only, like the rest."""
    assert client.get("/admin/models", headers=auth(member_key)).status_code == 403


# ---------------------------------------------------------------------------
# The server's own guard: an API client that has never heard of the field
# ---------------------------------------------------------------------------
def test_a_document_that_omits_managed_by_keeps_the_one_on_file(writable_config, client):
    """Saying nothing about a field is not the same as asking for it to go."""
    client.post("/admin/models", json=_definition(), headers=auth(client.admin_key))

    silent = _definition(managed=None)
    silent["metadata"]["display_name"] = "Saved by an older client"
    assert client.post(
        "/admin/models", json=silent, headers=auth(client.admin_key)
    ).status_code == 201

    assert _entry(client)["endpoints"][0].get("managed_by") == MANAGED


def test_asking_for_it_to_go_still_removes_it(writable_config, client):
    """A backend can stop being LMDS-managed, and the console must be able to say so."""
    client.post("/admin/models", json=_definition(), headers=auth(client.admin_key))

    explicit = _definition(managed=None)
    explicit["spec"]["endpoints"][0]["managed_by"] = None
    assert client.post(
        "/admin/models", json=explicit, headers=auth(client.admin_key)
    ).status_code == 201

    assert _entry(client)["endpoints"][0].get("managed_by") is None


def test_the_block_is_kept_per_backend_not_per_model(writable_config, client):
    """Two machines, one managed: the kept block must land on the right one."""
    document = _definition(alias="two-boxes")
    second = dict(document["spec"]["endpoints"][0])
    second.pop("managed_by", None)
    second = {**second, "name": "box2", "base_url": "http://otherbox:9000"}
    document["spec"]["endpoints"].append(second)
    client.post("/admin/models", json=document, headers=auth(client.admin_key))

    silent = _definition(alias="two-boxes", managed=None)
    silent["spec"]["endpoints"].append(second)
    client.post("/admin/models", json=silent, headers=auth(client.admin_key))

    endpoints = {e["name"]: e for e in _entry(client, "two-boxes")["endpoints"]}
    assert endpoints["box1"].get("managed_by") == MANAGED
    assert endpoints["box2"].get("managed_by") is None


# ---------------------------------------------------------------------------
# The console half, which has no runtime harness to catch it
# ---------------------------------------------------------------------------
def _js_function(name: str) -> str:
    """The source of one top-level function in app.js."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 1)
    return source[start:end if end != -1 else len(source)]


def test_the_console_carries_managed_by_back_to_the_server():
    """The form has no field for it, so only carrying it through preserves it.

    There is no browser harness here, and the failure this guards against is
    invisible at runtime: a Save that looks like it worked, with the deploy
    link gone from the file. Reading the two functions that make up the round
    trip is the cheapest check that the carry-through is still wired.
    """
    assert "managed_by" in _js_function("addEndpointRow"), (
        "addEndpointRow ไม่ได้เก็บ managed_by ไว้กับแถว — แก้โมเดลจากคอนโซลแล้ว "
        "ลิงก์ไป LMDS จะหายเงียบ ๆ"
    )
    assert "managed_by" in _js_function("readEndpoints"), (
        "readEndpoints ไม่ได้ส่ง managed_by กลับ — POST /admin/models เขียนทับทั้งเอกสาร"
    )
