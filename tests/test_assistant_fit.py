"""Which model should answer in the console panel, and why (FR-54)."""

from __future__ import annotations

import pytest

from app.core import assistant_fit
from app.registry.writer import validate_definition


def _model(**overrides):
    """A registry entry built the way the loader builds one, so the test cannot
    drift away from what the running system actually validates."""
    spec = {
        "upstream_model": "org/Model",
        "purpose": overrides.pop("purpose", ["general"]),
        "capabilities": {
            "chat": True,
            "streaming": True,
            **overrides.pop("capabilities", {}),
        },
        "limits": {
            "context_tokens": overrides.pop("context_tokens", 131072),
            "max_output_tokens": overrides.pop("max_output_tokens", 4096),
        },
        "endpoints": [
            {"name": "primary", "base_url": "http://dgx01:8000/v1", "server_type": "vllm"}
        ],
    }
    alias = overrides.pop("alias", "general")
    return validate_definition({
        "apiVersion": "litegate.dev/v1",
        "kind": "Model",
        "metadata": {
            "alias": alias,
            "display_name": overrides.pop("display_name", "General AI"),
        },
        "spec": spec,
    })


def _detail(fit) -> str:
    return " ".join(r.detail for r in fit.reasons)


# ---------------------------------------------------------------------------
# Blockers: the model cannot serve the role at all
# ---------------------------------------------------------------------------
def test_a_model_that_does_not_chat_is_blocked():
    fit = assistant_fit.assess(_model(capabilities={"chat": False}))
    assert fit.usable is False
    assert "cannot hold a conversation" in _detail(fit)


def test_a_small_context_is_blocked_because_the_state_block_would_not_fit():
    """The prompt is mostly state, so a short window leaves nothing for the chat."""
    fit = assistant_fit.assess(_model(context_tokens=8192))
    assert fit.usable is False
    assert "8,192" in _detail(fit)


def test_a_model_the_suite_could_not_reach_is_blocked():
    fit = assistant_fit.assess(_model(), compatibility={"chat": "fail"})
    assert fit.usable is False
    assert "could not get a chat reply" in _detail(fit)


def test_a_blocked_model_is_still_listed_with_its_reason():
    """"Why can I not pick that one?" deserves an answer, not an absence."""
    ranked = assistant_fit.rank([_model(alias="tiny", context_tokens=4096), _model(alias="ok")])
    assert [f.alias for f in ranked] == ["ok", "tiny"]
    assert ranked[-1].usable is False
    assert ranked[-1].blockers


# ---------------------------------------------------------------------------
# Narration: the worst trait for a small panel
# ---------------------------------------------------------------------------
def test_an_untested_reasoning_model_ranks_below_a_plain_chat_model():
    plain = assistant_fit.assess(_model(alias="plain"))
    thinker = assistant_fit.assess(_model(alias="thinker", capabilities={"reasoning": True}))
    assert plain.score > thinker.score


def test_a_reasoning_model_whose_backend_separates_the_thinking_is_fine():
    """--reasoning-parser is the real fix, and the ranking should reward it."""
    unseparated = assistant_fit.assess(
        _model(capabilities={"reasoning": True}),
        compatibility={"reasoning_separated": "fail"},
    )
    separated = assistant_fit.assess(
        _model(capabilities={"reasoning": True}),
        compatibility={"reasoning_separated": "pass"},
    )
    assert separated.score > unseparated.score
    assert "--reasoning-parser" in _detail(unseparated)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def test_a_general_model_outranks_a_specialist():
    general = assistant_fit.assess(_model(purpose=["general"]))
    coder = assistant_fit.assess(_model(purpose=["coding"]))
    assert general.score > coder.score
    assert "Specialist model" in _detail(coder)


def test_an_unhealthy_backend_sinks_a_model_without_disqualifying_it():
    """Health changes minute to minute; it should reorder, not blocklist."""
    fit = assistant_fit.assess(_model(), healthy=False)
    assert fit.usable is True
    assert fit.score < assistant_fit.assess(_model(), healthy=True).score


def test_a_model_that_was_never_routed_to_is_not_punished():
    """Unknown health is not bad health - a fresh gateway has routed nowhere."""
    unknown = assistant_fit.assess(_model(), healthy=None)
    unhealthy = assistant_fit.assess(_model(), healthy=False)
    assert unknown.score > unhealthy.score


def test_ranking_is_stable_between_calls():
    """A list that reshuffles on refresh looks broken even when it is not."""
    models = [_model(alias=a) for a in ("beta", "alpha", "gamma")]
    assert [f.alias for f in assistant_fit.rank(models)] == \
        [f.alias for f in assistant_fit.rank(models)]


def test_an_untested_model_says_so():
    fit = assistant_fit.assess(_model())
    assert "Never tested" in _detail(fit)


@pytest.mark.parametrize("tokens", [1, 128, 511])
def test_a_tiny_output_cap_is_a_warning_not_a_blocker(tokens):
    fit = assistant_fit.assess(_model(max_output_tokens=tokens))
    assert fit.usable is True
    assert "cut off mid-sentence" in _detail(fit)
