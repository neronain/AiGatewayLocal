"""Model-level routing rules (complements Router, which picks the *machine*).

Router answers "which backend serves this alias". This answers the question one
level up: "which model should answer a request shaped like this one".

Two facts drive every decision here and both come from the request itself, never
from guessing intent:

    estimated prompt size  ->  too long for this model? send it somewhere wider
                           ->  small enough to be busywork? let a small model take it

Claude Code makes both cases constant traffic: it ships very large contexts for
real work, and a stream of tiny calls (naming a session, summarising a heading)
that used to occupy a slot on the big model for no reason.

**Permissions and quota are deliberately not re-evaluated here.** They are checked
against the alias the member asked for, before routing runs. Re-checking against
the resolved model would let a routing rule silently widen or narrow what someone
may use; charging against it would make a member's bill depend on an admin's
internal plumbing. Routing here is the same kind of admin decision as repointing
an alias (PRD §6) - the member asked for `coding` and gets `coding`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.capability import (
    CONTEXT_TOLERANCE,
    validate_model_capabilities,
    validate_protocol,
)
from app.core.errors import GatewayError
from app.core.multimodal import RequestProfile
from app.core.tokens import estimate_prompt_tokens
from app.registry.schema import ModelDefinition

log = logging.getLogger(__name__)

# A chain longer than this is a configuration mistake, not a routing need. The
# visited set already makes cycles impossible; this bounds honest-but-silly depth.
MAX_HOPS = 4


@dataclass(frozen=True)
class RouteDecision:
    """Which model actually runs, and why - the reason is for logs and headers."""

    model: ModelDefinition
    reason: str | None = None
    hops: tuple[str, ...] = ()

    @property
    def rerouted(self) -> bool:
        return bool(self.hops)


def _can_serve(
    model: ModelDefinition, profile: RequestProfile, protocol: str
) -> bool:
    """Would this model accept the request if we sent it there?

    Routing must never turn a clean 400 into a confusing one. If the target
    cannot do vision and the request has an image, we leave the request where it
    was so the member gets the accurate error about the model they asked for.
    """
    if not model.spec.enabled:
        return False
    try:
        validate_protocol(model, protocol)
        validate_model_capabilities(model, profile)
    except GatewayError:
        return False
    return True


def _fits(model: ModelDefinition, prompt_tokens: int) -> bool:
    return prompt_tokens <= model.spec.limits.context_tokens * CONTEXT_TOLERANCE


def resolve_route(
    snapshot,
    model: ModelDefinition,
    profile: RequestProfile,
    protocol: str,
    requested_max_tokens: int | None = None,
) -> RouteDecision:
    """Pick the model that should serve this request. Never raises.

    Falls back to the requested model whenever a rule cannot be honoured, so a
    misconfigured target degrades to today's behaviour instead of an outage.
    """
    prompt_tokens = estimate_prompt_tokens(profile)
    current = model
    visited = {model.alias}
    hops: list[str] = []
    reason: str | None = None

    for _ in range(MAX_HOPS):
        rules = current.spec.routing
        target_alias: str | None = None
        why: str | None = None

        small = rules.small_prompt
        if (
            small
            and not hops  # only from the alias the member asked for
            and prompt_tokens < small.under_tokens
            and (
                small.max_output_tokens is None
                or requested_max_tokens is None
                or requested_max_tokens <= small.max_output_tokens
            )
        ):
            target_alias, why = small.target, "small-prompt"
        elif rules.overflow and not _fits(current, prompt_tokens):
            target_alias, why = rules.overflow, "overflow"

        if target_alias is None or target_alias in visited:
            if target_alias in visited:
                log.warning(
                    "routing loop avoided: %s -> %s already visited",
                    current.alias,
                    target_alias,
                )
            break

        candidate = snapshot.get(target_alias)
        if candidate is None or not _can_serve(candidate, profile, protocol):
            log.warning(
                "routing rule on '%s' points at '%s', which cannot serve this "
                "request - keeping '%s'",
                current.alias,
                target_alias,
                current.alias,
            )
            break

        # An overflow target that is no wider than what we have solves nothing and
        # would only move the 400 somewhere more confusing.
        if why == "overflow" and not _fits(candidate, prompt_tokens):
            log.warning(
                "overflow target '%s' is too small for ~%d tokens - keeping '%s'",
                target_alias,
                prompt_tokens,
                current.alias,
            )
            break

        visited.add(target_alias)
        hops.append(target_alias)
        reason = why
        current = candidate

    return RouteDecision(model=current, reason=reason, hops=tuple(hops))


def fallback_models(snapshot, model: ModelDefinition) -> list[ModelDefinition]:
    """Other models to try when no endpoint of `model` can take the request.

    Endpoint failover already covers "this machine is down". This covers "every
    machine behind this alias is down", which today is a 503 even when an
    equivalent model is idle on another node.
    """
    out: list[ModelDefinition] = []
    seen = {model.alias}
    for alias in model.spec.routing.fallback:
        if alias in seen:
            continue
        seen.add(alias)
        candidate = snapshot.get(alias)
        if candidate is not None:
            out.append(candidate)
    return out


def validate_routing(models: dict[str, ModelDefinition]) -> list[str]:
    """Cross-document checks, run once at load time (PRD §15: fail at load, not per request)."""
    errors: list[str] = []
    for alias, model in models.items():
        rules = model.spec.routing
        targets = [("overflow", rules.overflow)] if rules.overflow else []
        if rules.small_prompt:
            targets.append(("small_prompt.target", rules.small_prompt.target))
        targets += [("fallback", a) for a in rules.fallback]

        for field, target in targets:
            if target == alias:
                errors.append(f"{alias}: routing.{field} points at itself")
            elif target not in models:
                errors.append(f"{alias}: routing.{field} '{target}' is not a known alias")

        if rules.overflow and rules.overflow in models:
            here = model.spec.limits.context_tokens
            there = models[rules.overflow].spec.limits.context_tokens
            if there <= here:
                errors.append(
                    f"{alias}: routing.overflow '{rules.overflow}' has a "
                    f"{there:,}-token window, no larger than this model's {here:,} - "
                    "overflow would have nowhere to go"
                )
    return errors
