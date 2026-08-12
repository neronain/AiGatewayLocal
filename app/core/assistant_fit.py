"""How well a model suits the console assistant role (FR-54).

The assistant asks something unusual of a model. Its prompt is mostly *state* —
the catalogue, the caller's quota, backend health — and it grows with the
deployment, while the answers it should give are short. It is read in a small
panel next to whatever the operator was already doing, so a model that spends
two hundred tokens narrating its plan is worse than useless there even if the
answer at the end is correct.

None of that is visible from a model's name, and most of it is not visible from
its capability flags either. What it *is* visible from is the registry entry and
the compatibility record the test suite already writes. So this module reads
those and says, in words, why a model does or does not fit — the same reasoning
the automatic pick uses, so the console never recommends one model while quietly
running another.

Scores are for ordering, not for display as a grade. The reasons are the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.registry.schema import ModelDefinition

# The state block is capped at 12000 characters. Thai and Chinese tokenize far
# worse than English - roughly a token per character in the worst case - so the
# prompt is budgeted against that ceiling rather than an English estimate.
STATE_PROMPT_TOKENS = 12000
# Below this the state block alone would crowd out the conversation.
MIN_CONTEXT_TOKENS = 16384
# Above this there is room for the state, a dozen turns and a long answer.
COMFORTABLE_CONTEXT_TOKENS = 32768
# Short answers into a small panel. A model that cannot be given room for a
# couple of paragraphs will truncate mid-sentence.
MIN_OUTPUT_TOKENS = 512


@dataclass
class Reason:
    """One finding about a model's fit, in the operator's terms."""

    # good | warning | blocker - a blocker means the model cannot serve the role
    # at all, not merely that it serves it badly.
    kind: str
    detail: str


@dataclass
class Fit:
    alias: str
    display_name: str
    usable: bool
    score: int
    reasons: list[Reason] = field(default_factory=list)

    @property
    def blockers(self) -> list[Reason]:
        return [r for r in self.reasons if r.kind == "blocker"]

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "display_name": self.display_name,
            "usable": self.usable,
            "score": self.score,
            "reasons": [{"kind": r.kind, "detail": r.detail} for r in self.reasons],
        }


def assess(
    model: ModelDefinition,
    *,
    healthy: bool | None = None,
    compatibility: dict[str, str] | None = None,
) -> Fit:
    """Judge one model for the assistant role.

    `healthy` is the router's current view, `None` when it has not routed to
    this model yet — which is not the same as unhealthy and is not held against
    the model. `compatibility` maps feature -> status from the test suite; an
    untested model is judged on its declaration alone, and told so.
    """
    compatibility = compatibility or {}
    caps = model.spec.capabilities
    limits = model.spec.limits
    reasons: list[Reason] = []
    score = 0

    if not caps.chat:
        reasons.append(Reason("blocker", "Does not serve chat, so it cannot hold a conversation."))
        return Fit(model.alias, model.metadata.display_name, False, -1000, reasons)

    if compatibility.get("chat") == "fail":
        reasons.append(
            Reason("blocker", "The test suite could not get a chat reply from this backend.")
        )
        return Fit(model.alias, model.metadata.display_name, False, -1000, reasons)

    # --- context: the assistant's prompt is mostly state ---------------------
    if limits.context_tokens < MIN_CONTEXT_TOKENS:
        reasons.append(
            Reason(
                "blocker",
                f"Context is {limits.context_tokens:,} tokens. The state block alone can "
                f"reach ~{STATE_PROMPT_TOKENS:,}, leaving no room for the conversation.",
            )
        )
        return Fit(model.alias, model.metadata.display_name, False, -1000, reasons)

    if limits.context_tokens >= COMFORTABLE_CONTEXT_TOKENS:
        score += 30
        reasons.append(
            Reason("good", f"{limits.context_tokens:,}-token context — room for state and history.")
        )
    else:
        score += 10
        reasons.append(
            Reason(
                "warning",
                f"{limits.context_tokens:,}-token context works today, but the state block "
                "grows with the fleet. Watch it as you add models and nodes.",
            )
        )

    if limits.max_output_tokens < MIN_OUTPUT_TOKENS:
        score -= 20
        reasons.append(
            Reason(
                "warning",
                f"Output is capped at {limits.max_output_tokens} tokens; longer answers "
                "will be cut off mid-sentence.",
            )
        )

    # --- narration: the single worst trait for a small panel -----------------
    if caps.reasoning:
        separated = compatibility.get("reasoning_separated")
        if separated == "pass":
            score += 10
            reasons.append(
                Reason("good", "Reasoning model, but the backend separates the chain of thought.")
            )
        elif separated == "fail":
            score -= 40
            reasons.append(
                Reason(
                    "warning",
                    "Reasoning arrives inside the answer: this backend has no "
                    "--reasoning-parser, so the panel shows the model thinking out loud. "
                    "Restart it with the parser for this model family, or pick a plain "
                    "chat model.",
                )
            )
        else:
            score -= 15
            reasons.append(
                Reason(
                    "warning",
                    "Reasoning model, and nobody has tested whether this backend separates "
                    "the chain of thought. Run the test suite to find out.",
                )
            )
    else:
        score += 25
        reasons.append(Reason("good", "Plain chat model — answers without narrating."))

    # --- purpose: a specialist answers operations questions in its own accent -
    purposes = {p.value for p in model.spec.purpose}
    if purposes & {"general", "fast"}:
        score += 25
        reasons.append(Reason("good", "Meant for general use."))
    elif purposes & {"coding", "vision", "reasoning"}:
        score += 5
        reasons.append(
            Reason(
                "warning",
                f"Specialist model ({', '.join(sorted(purposes))}). It will answer, but a "
                "general model usually reads better in a support panel.",
            )
        )

    # --- can it actually be reached right now --------------------------------
    if healthy is False:
        score -= 60
        reasons.append(
            Reason("warning", "The backend is currently unhealthy — the assistant would fail.")
        )
    elif healthy is True:
        score += 20
        reasons.append(Reason("good", "Backend is healthy."))

    if not compatibility:
        reasons.append(
            Reason(
                "warning",
                "Never tested. This judgement rests on what the YAML declares, not on "
                "what the backend was measured to do.",
            )
        )

    # Streaming is not required - the panel handles a single block - but the
    # answer appearing a word at a time is most of what makes it feel fast.
    if caps.streaming:
        score += 10
    else:
        reasons.append(
            Reason(
                "warning",
                "No streaming: the panel waits in silence until the whole answer lands.",
            )
        )

    return Fit(model.alias, model.metadata.display_name, True, score, reasons)


def rank(
    models: list[ModelDefinition],
    *,
    health: dict[str, bool] | None = None,
    compatibility: dict[str, dict[str, str]] | None = None,
) -> list[Fit]:
    """Every candidate, best first. Unusable models are kept, with their reason.

    Kept rather than filtered because "why can I not choose that one?" is the
    question an operator actually asks, and a model missing from a list answers
    it with silence.
    """
    health = health or {}
    compatibility = compatibility or {}
    fits = [
        assess(
            model,
            healthy=health.get(model.alias),
            compatibility=compatibility.get(model.alias),
        )
        for model in models
    ]
    # Alias as the tiebreak so equal candidates keep a stable order between
    # calls; a list that reshuffles on refresh looks broken.
    return sorted(fits, key=lambda f: (-f.score, f.alias))
