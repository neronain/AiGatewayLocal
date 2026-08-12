"""Canonical model-registry schema (PRD v1.3 §4).

A model declares *what it can do*, not *what it is*. There is no `type` enum:
one model may be chat + vision + coding + agentic simultaneously.

An endpoint declares what the **serving backend** supports. A request is only
routed when BOTH the model and the endpoint satisfy the required capability
(PRD §14 "Backend Capability").
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class Purpose(StrEnum):
    """Catalogue grouping shown to students. A model may have several."""

    GENERAL = "general"
    CODING = "coding"
    VISION = "vision"
    REASONING = "reasoning"
    AGENT = "agent"
    FAST = "fast"
    EMBEDDING = "embedding"


class ServerType(StrEnum):
    VLLM = "vllm"
    OLLAMA = "ollama"
    SGLANG = "sglang"
    LLAMACPP = "llama.cpp"
    OPENAI_COMPATIBLE = "openai_compatible"


class Visibility(StrEnum):
    STUDENT = "student"
    INSTRUCTOR = "instructor"
    ADMIN = "admin"


class Capabilities(BaseModel):
    """Feature flags. Absent flag == not supported (fail closed)."""

    model_config = ConfigDict(extra="forbid")

    chat: bool = True
    vision: bool = False
    audio: bool = False
    coding: bool = False
    tools: bool = False
    streaming: bool = True
    reasoning: bool = False
    agentic: bool = False
    embedding: bool = False


class Protocols(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openai: bool = True
    anthropic: bool = False


class Modalities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    output: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)


class AgentClientProfile(BaseModel):
    """PRD §8 - compatibility is *tested*, never inferred from the model name."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    tested: bool = False
    notes: str = ""


class RemoteImageUrlPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)


class VisionPolicy(BaseModel):
    """PRD §12. Defined globally in gateway.yaml, optionally overridden per model."""

    model_config = ConfigDict(extra="forbid")

    max_images_per_request: int = Field(default=4, ge=0)
    max_image_size_mb: float = Field(default=10.0, ge=0)
    allowed_types: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/webp"]
    )
    remote_image_url: RemoteImageUrlPolicy = Field(default_factory=RemoteImageUrlPolicy)

    @property
    def max_image_size_bytes(self) -> int:
        return int(self.max_image_size_mb * 1024 * 1024)


class EndpointModalities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: bool = True
    image: bool = False
    audio: bool = False
    video: bool = False

    def supports(self, modality: Modality) -> bool:
        return bool(getattr(self, modality.value, False))


class Endpoint(BaseModel):
    """A concrete serving process (one vLLM/Ollama instance on one node)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    server_type: ServerType
    base_url: str
    # Optional override of spec.upstream_model. Needed when two backends serve
    # the same weights under different names - a router may expose
    # "Ai1/Qwen3-Coder-30B" for what the direct server calls "Qwen3-Coder-30B".
    # Without this an alias could not fail over between them.
    upstream_model: str = ""
    # Name of the env var holding the upstream key. The value is never written
    # to config files, logs, or the admin API.
    api_key_env: str = ""
    # Higher priority wins. Equal priority load-balances by weight.
    priority: int = 100
    weight: int = Field(default=1, ge=1)
    max_concurrency: int = Field(default=32, ge=1)
    protocols: Protocols = Field(default_factory=Protocols)
    modalities: EndpointModalities = Field(default_factory=EndpointModalities)
    health_path: str = "/health"
    enabled: bool = True

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.rstrip("/")


class ModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Stable public identifier. This is the ONLY name a student ever sees.
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$")
    display_name: str
    description: str = ""
    visibility: Visibility = Visibility.STUDENT
    tags: list[str] = Field(default_factory=list)


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Real upstream repository name. Admin-only (PRD §6).
    upstream_model: str
    purpose: list[Purpose] = Field(default_factory=lambda: [Purpose.GENERAL])
    limits: Limits
    modalities: Modalities = Field(default_factory=Modalities)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    protocols: Protocols = Field(default_factory=Protocols)
    agent_clients: dict[str, AgentClientProfile] = Field(default_factory=dict)
    vision_policy: VisionPolicy | None = None
    endpoints: list[Endpoint] = Field(min_length=1)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> ModelSpec:
        """Catch contradictory declarations at load time, not at request time."""
        if self.capabilities.vision and Modality.IMAGE not in self.modalities.input:
            raise ValueError(
                "capabilities.vision=true requires 'image' in modalities.input"
            )
        if Modality.IMAGE in self.modalities.input and not self.capabilities.vision:
            raise ValueError(
                "modalities.input contains 'image' but capabilities.vision=false"
            )
        if self.capabilities.audio and Modality.AUDIO not in self.modalities.input:
            raise ValueError(
                "capabilities.audio=true requires 'audio' in modalities.input"
            )

        enabled = [e for e in self.endpoints if e.enabled]
        if not enabled:
            raise ValueError("at least one endpoint must be enabled")

        # `spec.protocols` declares which gateway API surfaces this alias exposes;
        # `endpoint.protocols` declares what the backend natively speaks. The
        # gateway can bridge Anthropic -> OpenAI, but not the reverse, so the
        # two surfaces have different requirements.
        if self.protocols.openai and not any(e.protocols.openai for e in enabled):
            raise ValueError(
                "protocols.openai=true but no enabled endpoint speaks the OpenAI API"
            )
        if self.protocols.anthropic and not any(
            e.protocols.anthropic or e.protocols.openai for e in enabled
        ):
            raise ValueError(
                "protocols.anthropic=true requires an enabled endpoint speaking either "
                "the Anthropic API (native passthrough) or the OpenAI API (translated)"
            )

        # Same for modalities: model says vision, backend must serve images.
        if self.capabilities.vision and not any(e.modalities.image for e in enabled):
            raise ValueError(
                "capabilities.vision=true but no enabled endpoint has modalities.image"
            )

        names = [e.name for e in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("endpoint names must be unique within a model")

        return self


class ModelDefinition(BaseModel):
    """Top-level document of config/models/<alias>.yaml."""

    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal["edullm.gateway/v1"] = "edullm.gateway/v1"
    kind: Literal["Model"] = "Model"
    metadata: ModelMetadata
    spec: ModelSpec

    @property
    def alias(self) -> str:
        return self.metadata.alias


class QuotaDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: Literal["day", "month", "term"] = "day"
    max_requests: int = Field(default=1000, ge=0)
    max_input_tokens: int = Field(default=2_000_000, ge=0)
    max_output_tokens: int = Field(default=500_000, ge=0)
    max_images: int = Field(default=200, ge=0)


class RateLimitDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(default=60, ge=1)
    concurrent_requests: int = Field(default=4, ge=1)


class PrivacyPolicy(BaseModel):
    """PRD §11. Defaults are deliberately the most private option."""

    model_config = ConfigDict(extra="forbid")

    store_prompts: bool = False
    store_responses: bool = False
    store_images: bool = False


class GatewayConfig(BaseModel):
    """Top-level document of config/gateway.yaml."""

    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal["edullm.gateway/v1"] = "edullm.gateway/v1"
    kind: Literal["GatewayConfig"] = "GatewayConfig"
    vision_policy: VisionPolicy = Field(default_factory=VisionPolicy)
    privacy: PrivacyPolicy = Field(default_factory=PrivacyPolicy)
    quota_defaults: QuotaDefaults = Field(default_factory=QuotaDefaults)
    rate_limit_defaults: RateLimitDefaults = Field(default_factory=RateLimitDefaults)
    health_check_interval_seconds: int = Field(default=15, ge=5)
    health_check_timeout_seconds: float = Field(default=5.0, ge=0.5)
    unhealthy_threshold: int = Field(default=3, ge=1)
    healthy_threshold: int = Field(default=2, ge=1)
