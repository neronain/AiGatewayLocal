"""Sending a finding back to the tool that can fix it (FR-55).

The verification loop stopped one step short. LiteGate probes a backend, works
out that vLLM was started without `--tool-call-parser`, and prints the exact
command to fix it — and then somebody has to open a terminal, find the machine,
and paste it. The gap between "the system knows what is wrong and how to fix it"
and "the fix is applied" was a person copying a string.

This closes it, under three constraints that shape everything here:

* **Optional.** LiteGate has no idea what deployed its backends and mostly does
  not need to. With no LMDS configured, findings render exactly as before: a
  command you can paste. Nothing in the request path touches this module.
* **Per-endpoint.** The button appears only for endpoints whose `managed_by`
  says which LMDS node and bundle they came from. A gateway pointed at one LMDS
  can still have backends nobody manages, and guessing would send a restart to
  the wrong machine.
* **Narrow.** Exactly the fixes the probe can detect and verify afterwards. This
  is not a remote shell with a friendly name: the payload is a parser name that
  has already been matched against a pattern, sent to one endpoint, for one
  bundle, on one node.

The result is checkable: apply the fix, re-run the probe, and see whether the
finding is gone. That last part is what makes it a loop rather than a button.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.core.errors import ErrorCode, GatewayError
from app.registry.schema import ManagedBy

log = logging.getLogger(__name__)

# Settings keys, stored in gateway_settings so every worker sees the same value.
BASE_URL_KEY = "lmds_base_url"
TOKEN_KEY = "lmds_token"

# The findings that can be applied, and the option each one sets. Keeping this
# a table rather than a branch is the point: adding a remote action should be a
# deliberate line here, not something that follows from a new advice string.
APPLIABLE = {
    "tools_flag_missing": "tool_parser",
    "reasoning_not_separated": "reasoning_parser",
}

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)


@dataclass
class Connection:
    base_url: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


def suggest_reasoning_parser(served_name: str) -> tuple[str, bool]:
    """Return (parser, confident) for vLLM's --reasoning-parser.

    Same shape as the tool-parser hint and the same rule: say when it is a
    guess. A wrong reasoning parser does not error, it just quietly fails to
    separate anything, so an unmarked guess would look like a working fix.
    """
    name = (served_name or "").lower()
    for needles, parser in (
        (("deepseek-r1", "deepseek_r1", "r1-distill"), "deepseek_r1"),
        (("qwen3", "qwq"), "deepseek_r1"),  # Qwen3 emits <think>, same grammar
        (("granite",), "granite"),
        (("glm-4", "glm4"), "glm45"),
    ):
        if any(needle in name for needle in needles):
            return parser, True
    return "deepseek_r1", False


async def apply_fix(
    connection: Connection,
    managed: ManagedBy,
    issue: str,
    parser: str,
) -> dict:
    """Ask LMDS to restart one bundle with one parser set.

    Restart rather than a script edit: the parser is a knob those controllers
    already read from the environment, so this changes how the model is served
    without changing what is on disk. A bundle regenerated tomorrow keeps
    working.
    """
    option = APPLIABLE.get(issue)
    if option is None:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"'{issue}' is not a finding that can be applied automatically. "
            f"Appliable findings: {', '.join(sorted(APPLIABLE))}.",
        )
    if not connection.configured:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "No deploy tool is connected. Set one under Integrations, or run the "
            "command yourself.",
        )
    if managed.tool != "lmds":
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"This endpoint is managed by '{managed.tool or 'nothing'}', not LMDS.",
        )
    if not managed.lmds_node or not managed.lmds_slug:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "This endpoint does not say which LMDS machine and bundle it came "
            "from. Add `lmds_node` and `lmds_slug` under `managed_by` in the "
            "model file, or run the command yourself.",
        )

    url = (
        f"{connection.base_url.rstrip('/')}"
        f"/api/nodes/{managed.lmds_node}/models/{managed.lmds_slug}/restart"
    )
    headers = {"x-lmds-token": connection.token} if connection.token else {}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json={option: parser}, headers=headers)
    except httpx.HTTPError as exc:
        raise GatewayError(
            ErrorCode.UPSTREAM_ERROR, f"Could not reach the deploy tool: {exc}"
        ) from exc

    if response.status_code >= 400:
        # LMDS answers in the operator's language; pass it through rather than
        # replacing it with something vaguer of our own.
        detail = _detail(response)
        raise GatewayError(
            ErrorCode.UPSTREAM_ERROR,
            f"The deploy tool refused the change (HTTP {response.status_code}): {detail}",
        )

    log.info(
        "applied %s=%s to %s/%s via LMDS",
        option, parser, managed.lmds_node, managed.lmds_slug,
    )
    body = _body(response)
    return {
        "applied": {option: parser},
        "node": managed.lmds_node,
        "slug": managed.lmds_slug,
        # A restart is a long job over there; the console re-probes rather than
        # waiting, because "did it work" is a question only the probe answers.
        "job": body.get("job"),
        "output": (body.get("stdout") or body.get("detail") or "")[:2000],
    }


def _body(response: httpx.Response) -> dict:
    try:
        parsed = response.json()
    except ValueError:
        return {"detail": response.text[:2000]}
    return parsed if isinstance(parsed, dict) else {"detail": str(parsed)[:2000]}


def _detail(response: httpx.Response) -> str:
    detail = _body(response).get("detail") or response.text[:300]
    return str(detail)[:300]
