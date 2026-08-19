"""Model-level routing: overflow, small-prompt, fallback (app/core/rules.py).

These rules decide *which model* answers. The Router decides *which machine*.
Both are tested against real ModelDefinition objects rather than mocks, because
the whole point is that the rules obey the same capability gates as everything
else - a mock would happily prove the opposite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.multimodal import profile_openai_request
from app.core.rules import fallback_models, resolve_route, validate_routing
from app.registry.schema import ModelDefinition, VisionPolicy
from app.registry.store import RegistrySnapshot, load_snapshot
from tests.conftest import png_data_url

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="module")
def base():
    return load_snapshot(CONFIG)


def _model(source: ModelDefinition, alias: str, **spec_overrides) -> ModelDefinition:
    """Copy a real model under a new alias, overriding parts of its spec."""
    data = source.model_dump(mode="python")
    data["metadata"] = {**data["metadata"], "alias": alias}
    data["spec"] = {**data["spec"], **spec_overrides}
    return ModelDefinition.model_validate(data)


def _snapshot(base, *models: ModelDefinition) -> RegistrySnapshot:
    return RegistrySnapshot(
        gateway=base.gateway, models={m.alias: m for m in models}
    )


def _profile(text: str, *, image: bool = False):
    content = [{"type": "text", "text": text}]
    if image:
        content.append({"type": "image_url", "image_url": {"url": png_data_url()}})
    return profile_openai_request(
        {"model": "x", "messages": [{"role": "user", "content": content}]}, VisionPolicy()
    )


# ── overflow ────────────────────────────────────────────────────────────────
def test_overflow_sends_an_over_long_prompt_to_a_wider_model(base):
    """เดิม prompt ยาวเกิน window = 400 ทิ้ง ทั้งที่มีเครื่องรับไหวอยู่"""
    wide = _model(base.get("coding"), "coding-long", limits={"context_tokens": 262144})
    narrow = _model(
        base.get("coding"), "coding",
        limits={"context_tokens": 2000},
        routing={"overflow": "coding-long"},
    )
    snap = _snapshot(base, narrow, wide)

    decision = resolve_route(snap, narrow, _profile("ก" * 200_000), "openai")

    assert decision.model.alias == "coding-long"
    assert decision.reason == "overflow"
    assert decision.rerouted


def test_a_prompt_that_fits_is_left_alone(base):
    wide = _model(base.get("coding"), "coding-long", limits={"context_tokens": 262144})
    narrow = _model(
        base.get("coding"), "coding",
        limits={"context_tokens": 262144},
        routing={"overflow": "coding-long"},
    )
    decision = resolve_route(_snapshot(base, narrow, wide), narrow, _profile("สั้น"), "openai")
    assert decision.model.alias == "coding"
    assert not decision.rerouted


def test_overflow_target_that_is_also_too_small_is_refused(base):
    """ย้ายไปเจอ 400 ที่โมเดลซึ่งผู้ใช้ไม่ได้ขอ คือทำให้ error สับสนกว่าเดิม"""
    also_small = _model(base.get("coding"), "coding-long", limits={"context_tokens": 3000})
    narrow = _model(
        base.get("coding"), "coding",
        limits={"context_tokens": 2000},
        routing={"overflow": "coding-long"},
    )
    decision = resolve_route(
        _snapshot(base, narrow, also_small), narrow, _profile("ก" * 200_000), "openai"
    )
    assert decision.model.alias == "coding"


def test_routing_never_lands_on_a_model_that_cannot_serve_the_request(base):
    """โมเดลปลายทางรับภาพไม่ได้ → ต้องคง 400 ที่ตรงกับ alias ที่ผู้ใช้ขอจริง"""
    text_only = _model(
        base.get("coding"), "coding-long",
        limits={"context_tokens": 262144},
    )
    vision = _model(
        base.get("gemma-vision"), "vision",
        limits={"context_tokens": 2000},
        routing={"overflow": "coding-long"},
    )
    decision = resolve_route(
        _snapshot(base, vision, text_only),
        vision,
        _profile("ก" * 200_000, image=True),
        "openai",
    )
    assert decision.model.alias == "vision"


# ── small prompt ────────────────────────────────────────────────────────────
def test_small_prompt_goes_to_the_small_model(base):
    """Claude Code ยิงงานจุกจิกถี่ ๆ — ไม่ควรกิน slot ของตัวใหญ่"""
    quick = _model(base.get("coding"), "quick")
    big = _model(
        base.get("coding"), "coding",
        routing={"small_prompt": {"under_tokens": 500, "target": "quick"}},
    )
    decision = resolve_route(_snapshot(base, big, quick), big, _profile("ตั้งชื่อ session"), "openai")
    assert decision.model.alias == "quick"
    assert decision.reason == "small-prompt"


def test_small_prompt_rule_yields_when_a_long_answer_is_asked_for(base):
    """prompt สั้นไม่ได้แปลว่างานเบา — 'เขียนบทความ 3000 คำ' ก็ prompt สั้น"""
    quick = _model(base.get("coding"), "quick")
    big = _model(
        base.get("coding"), "coding",
        routing={
            "small_prompt": {"under_tokens": 500, "max_output_tokens": 512, "target": "quick"}
        },
    )
    snap = _snapshot(base, big, quick)
    assert resolve_route(snap, big, _profile("สั้น"), "openai", 4096).model.alias == "coding"
    assert resolve_route(snap, big, _profile("สั้น"), "openai", 256).model.alias == "quick"


# ── loops and bad config ────────────────────────────────────────────────────
def test_a_routing_cycle_cannot_hang_the_request(base):
    loop_a = _model(base.get("coding"), "loop-a", limits={"context_tokens": 100},
                    routing={"overflow": "loop-b"})
    loop_b = _model(base.get("coding"), "loop-b", limits={"context_tokens": 100},
                    routing={"overflow": "loop-a"})
    decision = resolve_route(
        _snapshot(base, loop_a, loop_b), loop_a, _profile("ก" * 50_000), "openai"
    )
    assert decision.model.alias in {"loop-a", "loop-b"}
    assert len(decision.hops) <= 1


def test_a_target_that_does_not_exist_degrades_to_todays_behaviour(base):
    lone = _model(base.get("coding"), "coding", limits={"context_tokens": 100},
                  routing={"overflow": "ไม่มีอยู่จริง"})
    decision = resolve_route(_snapshot(base, lone), lone, _profile("ก" * 50_000), "openai")
    assert decision.model.alias == "coding"


def test_bad_routing_config_is_reported_at_load_time(base):
    """PRD §15: ผิดตั้งแต่ไฟล์ ต้องรู้ตอนโหลด ไม่ใช่ตอนผู้ใช้ยิงเข้ามา"""
    small = _model(base.get("coding"), "small", limits={"context_tokens": 200000},
                   routing={"overflow": "narrow"})
    narrow = _model(base.get("coding"), "narrow", limits={"context_tokens": 1000})
    selfref = _model(base.get("coding"), "selfref", routing={"fallback": ["selfref"]})
    ghost = _model(base.get("coding"), "ghost", routing={"fallback": ["nope"]})

    errors = validate_routing(
        {m.alias: m for m in (small, narrow, selfref, ghost)}
    )
    joined = " | ".join(errors)
    assert "no larger" in joined
    assert "points at itself" in joined
    assert "not a known alias" in joined


def test_the_shipped_registry_has_no_routing_errors(base):
    assert base.errors == []


# ── fallback ────────────────────────────────────────────────────────────────
def test_fallback_lists_only_models_that_exist(base):
    backup = _model(base.get("coding"), "coding-backup")
    main = _model(base.get("coding"), "coding",
                  routing={"fallback": ["coding-backup", "ไม่มี", "coding"]})
    chain = fallback_models(_snapshot(base, main, backup), main)
    assert [m.alias for m in chain] == ["coding-backup"]


def test_no_fallback_configured_means_no_change_in_behaviour(base):
    main = base.get("coding")
    assert fallback_models(base, main) == []
