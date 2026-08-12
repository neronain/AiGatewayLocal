"""Capability validation: the PRD §4 contract, tested against the real registry."""

from __future__ import annotations

import pytest

from app.core.capability import validate_context_budget, validate_model_capabilities
from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import RequestProfile, profile_openai_request
from app.registry.schema import VisionPolicy
from app.registry.store import load_snapshot
from tests.conftest import png_data_url


@pytest.fixture(scope="module")
def snapshot(request):
    from pathlib import Path

    return load_snapshot(Path(__file__).resolve().parent.parent / "config")


def test_registry_loads_without_errors(snapshot):
    assert snapshot.errors == []
    assert {"muse-local", "gemma-vision", "coding"} <= set(snapshot.models)


def test_image_to_text_only_model_is_rejected(snapshot):
    """PRD §4: model 'coding' has vision=false, so an image is a 400 from us."""
    coding = snapshot.get("coding")
    body = {
        "model": "coding",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "อธิบายภาพนี้"},
                    {"type": "image_url", "image_url": {"url": png_data_url()}},
                ],
            }
        ],
    }
    profile = profile_openai_request(body, VisionPolicy())
    with pytest.raises(GatewayError) as exc:
        validate_model_capabilities(coding, profile)
    assert exc.value.code == ErrorCode.MODEL_CAPABILITY_NOT_SUPPORTED
    assert exc.value.http_status == 400
    assert "does not support image input" in exc.value.message


def test_image_to_vision_model_is_accepted(snapshot):
    gemma = snapshot.get("gemma-vision")
    body = {
        "model": "gemma-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": png_data_url()}},
                ],
            }
        ],
    }
    profile = profile_openai_request(body, VisionPolicy())
    validate_model_capabilities(gemma, profile)  # must not raise
    assert profile.image_count == 1
    assert profile.request_modality == "text+image"


def test_tools_requirement_is_detected(snapshot):
    body = {
        "model": "coding",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }
    profile = profile_openai_request(body, VisionPolicy())
    assert profile.requires_tools
    validate_model_capabilities(snapshot.get("coding"), profile)


def test_context_budget_rejects_oversized_prompt(snapshot):
    model = snapshot.get("coding")
    profile = RequestProfile()
    profile.text_chars = model.spec.limits.context_tokens * 10  # far over
    with pytest.raises(GatewayError) as exc:
        validate_context_budget(model, profile, None)
    assert exc.value.code == ErrorCode.CONTEXT_LENGTH_EXCEEDED


def test_context_budget_clamps_max_tokens(snapshot):
    model = snapshot.get("coding")
    profile = RequestProfile()
    profile.text_chars = 100
    effective = validate_context_budget(model, profile, 999_999)
    assert effective == model.spec.limits.max_output_tokens


def test_model_and_endpoint_capability_must_both_pass(snapshot):
    """PRD §14: a vision model on a text-only backend must not be routable."""
    gemma = snapshot.get("gemma-vision")
    endpoint = gemma.spec.endpoints[0]
    assert endpoint.modalities.image is True

    from app.core.capability import endpoint_supports

    profile = RequestProfile(modalities={"text", "image"})
    assert endpoint_supports(endpoint, profile, "openai") is True

    coding_endpoint = snapshot.get("coding").spec.endpoints[0]
    assert endpoint_supports(coding_endpoint, profile, "openai") is False
