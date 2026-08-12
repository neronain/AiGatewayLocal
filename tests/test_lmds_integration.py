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
