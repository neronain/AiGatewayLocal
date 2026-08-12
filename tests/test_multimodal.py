"""Vision policy enforcement (PRD §12) and content-block parsing (PRD §3)."""

from __future__ import annotations

import base64

import pytest

from app.core.errors import ErrorCode, GatewayError
from app.core.multimodal import (
    decode_data_url,
    image_dimensions,
    profile_anthropic_request,
    profile_openai_request,
)
from app.core.tokens import estimate_image_tokens, resolve_usage
from app.registry.schema import RemoteImageUrlPolicy, VisionPolicy
from tests.conftest import make_png, png_data_url


def test_png_dimensions_are_read_from_header():
    assert image_dimensions(make_png(320, 200)) == (320, 200)


def test_base64_image_is_accepted_and_measured():
    policy = VisionPolicy()
    ref = decode_data_url(png_data_url(128, 96), policy)
    assert ref.mime == "image/png"
    assert (ref.width, ref.height) == (128, 96)
    assert ref.source == "base64"


def test_disallowed_mime_is_rejected_by_magic_bytes_not_by_label():
    """A GIF mislabelled as PNG must still be rejected."""
    policy = VisionPolicy(allowed_types=["image/png"])
    gif = base64.b64encode(b"GIF89a" + b"\x00" * 32).decode()
    with pytest.raises(GatewayError) as exc:
        decode_data_url(f"data:image/png;base64,{gif}", policy)
    assert exc.value.code == ErrorCode.IMAGE_TYPE_NOT_ALLOWED
    assert exc.value.details["detected_type"] == "image/gif"


def test_oversized_image_is_rejected():
    # 100 bytes: smaller than any valid PNG, including a fully compressible one.
    policy = VisionPolicy(max_image_size_mb=100 / (1024 * 1024))
    assert len(make_png(256, 256)) > policy.max_image_size_bytes
    with pytest.raises(GatewayError) as exc:
        decode_data_url(png_data_url(256, 256), policy)
    assert exc.value.code == ErrorCode.IMAGE_TOO_LARGE
    assert exc.value.http_status == 413


def test_too_many_images_is_rejected():
    policy = VisionPolicy(max_images_per_request=2)
    body = {
        "model": "gemma-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": png_data_url()}}
                    for _ in range(3)
                ],
            }
        ],
    }
    with pytest.raises(GatewayError) as exc:
        profile_openai_request(body, policy)
    assert exc.value.code == ErrorCode.TOO_MANY_IMAGES


def test_remote_url_is_disabled_by_default():
    body = {
        "model": "gemma-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}
                ],
            }
        ],
    }
    with pytest.raises(GatewayError) as exc:
        profile_openai_request(body, VisionPolicy())
    assert exc.value.code == ErrorCode.REMOTE_IMAGE_URL_DISABLED


def test_remote_url_allow_list_is_enforced_when_enabled():
    policy = VisionPolicy(
        remote_image_url=RemoteImageUrlPolicy(enabled=True, allowed_hosts=["lms.university.ac.th"])
    )
    ok = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://lms.university.ac.th/a.png"},
                    }
                ],
            }
        ],
    }
    assert profile_openai_request(ok, policy).image_count == 1

    bad = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest"}}
                ],
            }
        ],
    }
    with pytest.raises(GatewayError) as exc:
        profile_openai_request(bad, policy)
    assert exc.value.code == ErrorCode.REMOTE_IMAGE_URL_DISABLED


def test_anthropic_image_block_is_parsed():
    body = {
        "model": "gemma-vision",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(make_png(64, 64)).decode(),
                        },
                    },
                ],
            }
        ],
    }
    profile = profile_anthropic_request(body, VisionPolicy())
    assert profile.image_count == 1
    assert "image" in profile.modalities


def test_anthropic_tool_result_images_are_counted():
    """A screenshot returned by a tool still consumes image quota."""
    body = {
        "model": "gemma-vision",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(make_png()).decode(),
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }
    profile = profile_anthropic_request(body, VisionPolicy())
    assert profile.image_count == 1
    assert profile.requires_tools


def test_unknown_content_block_type_is_rejected():
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "hologram", "data": "x"}]}],
    }
    with pytest.raises(GatewayError) as exc:
        profile_openai_request(body, VisionPolicy())
    assert exc.value.code == ErrorCode.INVALID_CONTENT_BLOCK


def test_visual_tokens_scale_with_image_size():
    small = estimate_image_tokens(
        decode_data_url(png_data_url(64, 64), VisionPolicy())
    )
    large = estimate_image_tokens(
        decode_data_url(png_data_url(1024, 1024), VisionPolicy())
    )
    assert large > small > 0


def test_usage_split_attributes_visual_tokens():
    body = {
        "model": "gemma-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x" * 320},
                    {"type": "image_url", "image_url": {"url": png_data_url(512, 512)}},
                ],
            }
        ],
    }
    profile = profile_openai_request(body, VisionPolicy())
    usage = resolve_usage(profile, {"prompt_tokens": 2000, "completion_tokens": 50})
    assert usage.accounting == "upstream"
    assert usage.visual_input_tokens > 0
    assert usage.text_input_tokens == 2000 - usage.visual_input_tokens
    assert usage.output_tokens == 50


def test_usage_falls_back_to_estimate_without_upstream_numbers():
    body = {"model": "coding", "messages": [{"role": "user", "content": "x" * 320}]}
    profile = profile_openai_request(body, VisionPolicy())
    usage = resolve_usage(profile, None)
    assert usage.accounting == "estimated"
    assert usage.text_input_tokens == 100  # 320 chars / 3.2
