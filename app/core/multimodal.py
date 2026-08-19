"""Multimodal content-block parsing and policy validation (PRD §3, §12, §13).

What this module does:
  * understands OpenAI `content: [{type: text|image_url}, ...]` blocks
  * understands Anthropic `content: [{type: text|image}, ...]` blocks
  * measures and validates images against the vision policy
  * reports which capabilities the request actually requires

What it deliberately does NOT do (PRD §13): resize, re-encode, OCR, detect
objects, or fetch remote URLs. The original bytes are forwarded untouched to the
model server. Keeping this rule is what keeps the gateway small.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.errors import ErrorCode, GatewayError
from app.registry.schema import VisionPolicy

# data:image/png;base64,AAAA...
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<params>;[^,]*)?,", re.I)

# Magic bytes -> canonical MIME. The client-declared type is advisory only; we
# trust the bytes, so a .png header cannot smuggle in an unlisted format.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


@dataclass
class ImageRef:
    """One image found in a request. `data` is held only for the request's life."""

    source: str  # "base64" | "url"
    mime: str
    size_bytes: int
    url: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class RequestProfile:
    """What the request needs, derived from its content - never from the model name."""

    modalities: set[str] = field(default_factory=lambda: {"text"})
    images: list[ImageRef] = field(default_factory=list)
    requires_tools: bool = False
    requires_streaming: bool = False
    text_chars: int = 0

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def has_images(self) -> bool:
        return bool(self.images)

    @property
    def request_modality(self) -> str:
        """Compact label stored on the usage row, e.g. 'text+image'."""
        order = ["text", "image", "audio", "video"]
        present = [m for m in order if m in self.modalities]
        return "+".join(present) or "text"

    def required_capabilities(self) -> list[str]:
        caps = []
        if "image" in self.modalities:
            caps.append("vision")
        if "audio" in self.modalities:
            caps.append("audio")
        if self.requires_tools:
            caps.append("tools")
        if self.requires_streaming:
            caps.append("streaming")
        return caps


def _sniff_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # WEBP: "RIFF" .... "WEBP"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG segments to the SOFn frame header. Pure stdlib, no decode."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + seg_len
    return None


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Header-only dimension read; used for visual-token estimation (PRD §17)."""
    for reader in (_png_dimensions, _jpeg_dimensions):
        dims = reader(data)
        if dims:
            return dims
    return None, None


def decode_data_url(url: str, policy: VisionPolicy) -> ImageRef:
    """Validate and measure a base64 data: URL without persisting it."""
    match = _DATA_URL_RE.match(url)
    if not match:
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK,
            "Malformed data URL. Expected 'data:image/<type>;base64,<data>'.",
        )
    params = match.group("params") or ""
    if "base64" not in params.lower():
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK,
            "Only base64-encoded data URLs are supported.",
        )

    payload = url[match.end() :]
    # Reject on declared size before decoding, so an oversized payload never
    # costs us the memory of a full decode. base64 inflates by 4/3.
    approx_bytes = (len(payload) * 3) // 4
    if approx_bytes > policy.max_image_size_bytes:
        raise GatewayError(
            ErrorCode.IMAGE_TOO_LARGE,
            f"Image exceeds the {policy.max_image_size_mb:g} MB limit "
            f"(~{approx_bytes / 1024 / 1024:.1f} MB).",
            details={"max_image_size_mb": policy.max_image_size_mb},
        )

    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK, f"Invalid base64 image data: {exc}"
        ) from exc

    if len(data) > policy.max_image_size_bytes:
        raise GatewayError(
            ErrorCode.IMAGE_TOO_LARGE,
            f"Image exceeds the {policy.max_image_size_bytes / 1024 / 1024:g} MB limit.",
        )

    sniffed = _sniff_mime(data)
    declared = (match.group("mime") or "").lower()
    mime = sniffed or declared
    if not mime:
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK, "Could not determine the image type."
        )
    if mime not in policy.allowed_types:
        raise GatewayError(
            ErrorCode.IMAGE_TYPE_NOT_ALLOWED,
            f"Image type '{mime}' is not allowed. Allowed: {', '.join(policy.allowed_types)}.",
            details={"detected_type": mime, "allowed_types": policy.allowed_types},
        )

    width, height = image_dimensions(data)
    return ImageRef(
        source="base64", mime=mime, size_bytes=len(data), width=width, height=height
    )


def validate_remote_url(url: str, policy: VisionPolicy) -> ImageRef:
    """PRD §12: remote fetch is off by default so the gateway is not an SSRF proxy."""
    if not policy.remote_image_url.enabled:
        raise GatewayError(
            ErrorCode.REMOTE_IMAGE_URL_DISABLED,
            "Remote image URLs are disabled. Send the image as a base64 data URL instead.",
        )
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK,
            f"Unsupported image URL scheme '{parsed.scheme}'.",
        )
    allowed_hosts = policy.remote_image_url.allowed_hosts
    if allowed_hosts and parsed.hostname not in allowed_hosts:
        raise GatewayError(
            ErrorCode.REMOTE_IMAGE_URL_DISABLED,
            f"Image host '{parsed.hostname}' is not on the allow-list.",
            details={"allowed_hosts": allowed_hosts},
        )
    return ImageRef(source="url", mime="image/*", size_bytes=0, url=url)


def _handle_image_source(url: str, policy: VisionPolicy) -> ImageRef:
    return (
        decode_data_url(url, policy)
        if url.startswith("data:")
        else validate_remote_url(url, policy)
    )


def profile_openai_request(body: dict[str, Any], policy: VisionPolicy) -> RequestProfile:
    """Inspect an OpenAI /v1/chat/completions body. Body is not mutated."""
    profile = RequestProfile()
    profile.requires_streaming = bool(body.get("stream"))
    if body.get("tools") or body.get("functions") or body.get("tool_choice"):
        profile.requires_tools = True

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "'messages' must be a non-empty array.", param="messages"
        )

    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GatewayError(
                ErrorCode.INVALID_REQUEST,
                f"messages[{idx}] must be an object.",
                param=f"messages[{idx}]",
            )
        if message.get("tool_calls"):
            profile.requires_tools = True

        content = message.get("content")
        if isinstance(content, str):
            profile.text_chars += len(content)
            continue
        if content is None:
            continue
        if not isinstance(content, list):
            raise GatewayError(
                ErrorCode.INVALID_CONTENT_BLOCK,
                f"messages[{idx}].content must be a string or an array of content blocks.",
                param=f"messages[{idx}].content",
            )

        for b_idx, block in enumerate(content):
            path = f"messages[{idx}].content[{b_idx}]"
            if not isinstance(block, dict):
                raise GatewayError(
                    ErrorCode.INVALID_CONTENT_BLOCK, f"{path} must be an object.", param=path
                )
            btype = block.get("type")
            if btype == "text":
                profile.text_chars += len(block.get("text") or "")
            elif btype == "image_url":
                image_url = block.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if not isinstance(url, str) or not url:
                    raise GatewayError(
                        ErrorCode.INVALID_CONTENT_BLOCK,
                        f"{path}.image_url.url is required.",
                        param=f"{path}.image_url.url",
                    )
                profile.modalities.add("image")
                profile.images.append(_handle_image_source(url, policy))
            elif btype in {"input_audio", "audio"}:
                profile.modalities.add("audio")
            elif btype in {"video", "input_video"}:
                profile.modalities.add("video")
            else:
                raise GatewayError(
                    ErrorCode.INVALID_CONTENT_BLOCK,
                    f"{path}.type '{btype}' is not supported.",
                    param=f"{path}.type",
                )

    _enforce_image_count(profile, policy)
    return profile


def profile_responses_request(body: dict[str, Any], policy: VisionPolicy) -> RequestProfile:
    """Inspect an OpenAI Responses body (/v1/responses) — Codex's native shape.

    `input` is either a bare string or a list of items, and the items are not all
    messages: a turn that used tools comes back as `function_call` /
    `function_call_output` items sitting at the same level as the messages. Reading
    only `{role, content}` would silently drop the entire tool history from the
    size estimate, which is exactly the traffic that makes Codex conversations
    long in the first place.
    """
    profile = RequestProfile()
    profile.requires_streaming = bool(body.get("stream"))
    if body.get("tools") or body.get("tool_choice"):
        profile.requires_tools = True

    instructions = body.get("instructions")
    if isinstance(instructions, str):
        profile.text_chars += len(instructions)

    payload = body.get("input")
    if isinstance(payload, str):
        if not payload:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, "'input' must not be empty.", param="input"
            )
        profile.text_chars += len(payload)
        return profile

    if not isinstance(payload, list) or not payload:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "'input' must be a string or a non-empty array of items.",
            param="input",
        )

    for idx, item in enumerate(payload):
        path = f"input[{idx}]"
        if not isinstance(item, dict):
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, f"{path} must be an object.", param=path
            )

        itype = item.get("type")
        if itype in {"function_call", "function_call_output"}:
            profile.requires_tools = True
            profile.text_chars += len(str(item.get("arguments") or item.get("output") or ""))
            continue

        content = item.get("content")
        if isinstance(content, str):
            profile.text_chars += len(content)
            continue
        if content is None:
            continue
        if not isinstance(content, list):
            raise GatewayError(
                ErrorCode.INVALID_CONTENT_BLOCK,
                f"{path}.content must be a string or an array of content parts.",
                param=f"{path}.content",
            )

        for p_idx, part in enumerate(content):
            ppath = f"{path}.content[{p_idx}]"
            if not isinstance(part, dict):
                raise GatewayError(
                    ErrorCode.INVALID_CONTENT_BLOCK, f"{ppath} must be an object.", param=ppath
                )
            ptype = part.get("type")
            if ptype in {"input_text", "output_text", "text"}:
                profile.text_chars += len(part.get("text") or "")
            elif ptype == "input_image":
                url = part.get("image_url")
                if isinstance(url, dict):  # tolerated: some clients send the chat shape
                    url = url.get("url")
                if not isinstance(url, str) or not url:
                    raise GatewayError(
                        ErrorCode.INVALID_CONTENT_BLOCK,
                        f"{ppath}.image_url is required for input_image.",
                        param=ppath,
                    )
                profile.modalities.add("image")
                profile.images.append(_handle_image_source(url, policy))
            elif ptype in {"input_audio", "input_file"}:
                # เจตนาไม่รองรับ: บอกให้ชัดดีกว่าเงียบแล้วส่งของที่ backend อ่านไม่ออกไป
                raise GatewayError(
                    ErrorCode.INVALID_CONTENT_BLOCK,
                    f"{ppath}: '{ptype}' is not supported on this gateway.",
                    param=ppath,
                )

    return profile


def profile_anthropic_request(body: dict[str, Any], policy: VisionPolicy) -> RequestProfile:
    """Inspect an Anthropic /v1/messages body (Claude Code's native shape)."""
    profile = RequestProfile()
    profile.requires_streaming = bool(body.get("stream"))
    if body.get("tools") or body.get("tool_choice"):
        profile.requires_tools = True

    system = body.get("system")
    if isinstance(system, str):
        profile.text_chars += len(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                profile.text_chars += len(block.get("text") or "")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST, "'messages' must be a non-empty array.", param="messages"
        )

    for idx, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            profile.text_chars += len(content)
            continue
        if not isinstance(content, list):
            raise GatewayError(
                ErrorCode.INVALID_CONTENT_BLOCK,
                f"messages[{idx}].content must be a string or an array of content blocks.",
                param=f"messages[{idx}].content",
            )
        for b_idx, block in enumerate(content):
            path = f"messages[{idx}].content[{b_idx}]"
            if not isinstance(block, dict):
                raise GatewayError(
                    ErrorCode.INVALID_CONTENT_BLOCK, f"{path} must be an object.", param=path
                )
            btype = block.get("type")
            if btype == "text":
                profile.text_chars += len(block.get("text") or "")
            elif btype == "image":
                profile.modalities.add("image")
                profile.images.append(_profile_anthropic_image(block, policy, path))
            elif btype in {"tool_use", "tool_result"}:
                profile.requires_tools = True
                # tool_result may itself carry images.
                nested = block.get("content")
                if isinstance(nested, list):
                    for n_idx, nested_block in enumerate(nested):
                        if (
                            isinstance(nested_block, dict)
                            and nested_block.get("type") == "image"
                        ):
                            profile.modalities.add("image")
                            profile.images.append(
                                _profile_anthropic_image(
                                    nested_block, policy, f"{path}.content[{n_idx}]"
                                )
                            )
            elif btype in {"document", "thinking", "redacted_thinking"}:
                continue
            else:
                raise GatewayError(
                    ErrorCode.INVALID_CONTENT_BLOCK,
                    f"{path}.type '{btype}' is not supported.",
                    param=f"{path}.type",
                )

    _enforce_image_count(profile, policy)
    return profile


def _profile_anthropic_image(
    block: dict[str, Any], policy: VisionPolicy, path: str
) -> ImageRef:
    source = block.get("source")
    if not isinstance(source, dict):
        raise GatewayError(
            ErrorCode.INVALID_CONTENT_BLOCK, f"{path}.source is required.", param=path
        )
    stype = source.get("type")
    if stype == "base64":
        media_type = source.get("media_type", "")
        data = source.get("data", "")
        if not isinstance(data, str) or not data:
            raise GatewayError(
                ErrorCode.INVALID_CONTENT_BLOCK,
                f"{path}.source.data is required.",
                param=path,
            )
        return decode_data_url(f"data:{media_type};base64,{data}", policy)
    if stype == "url":
        url = source.get("url", "")
        if not isinstance(url, str) or not url:
            raise GatewayError(
                ErrorCode.INVALID_CONTENT_BLOCK, f"{path}.source.url is required.", param=path
            )
        return validate_remote_url(url, policy)
    raise GatewayError(
        ErrorCode.INVALID_CONTENT_BLOCK,
        f"{path}.source.type '{stype}' is not supported.",
        param=path,
    )


def _enforce_image_count(profile: RequestProfile, policy: VisionPolicy) -> None:
    if profile.image_count > policy.max_images_per_request:
        raise GatewayError(
            ErrorCode.TOO_MANY_IMAGES,
            f"Request contains {profile.image_count} images; the limit is "
            f"{policy.max_images_per_request}.",
            details={
                "image_count": profile.image_count,
                "max_images_per_request": policy.max_images_per_request,
            },
        )
