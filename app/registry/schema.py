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

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class Purpose(StrEnum):
    """Catalogue grouping shown to members. A model may have several."""

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

    # ── ผู้ให้บริการออนไลน์ ──
    #
    # เป็นป้ายกำกับเหมือนชนิดอื่นในนี้ (ไม่มีอะไร branch จาก server_type) แต่มีเพื่อสองอย่าง:
    # หน้าเว็บเติม base URL / health path ให้เองได้ และ upstream_headers รู้ว่าคีย์ของเจ้านี้
    # ส่งด้วย header แบบไหน — Anthropic ใช้ x-api-key ไม่ใช่ Authorization: Bearer
    GEMINI = "gemini"
    MINIMAX = "minimax"
    # คนละบัญชี คนละคีย์ คนละโดเมน — ไม่ใช่แค่ URL สำรองของอันบน
    MINIMAX_CN = "minimax-cn"
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    TOGETHER = "together"


class Visibility(StrEnum):
    MEMBER = "member"
    MANAGER = "manager"
    ADMIN = "admin"

    @classmethod
    def _missing_(cls, value: object) -> Visibility | None:
        """Accept the pre-rename vocabulary.

        Registries written as `visibility: student` predate the move to
        workspace vocabulary. Rejecting them empties the registry on upgrade,
        which is exactly what happened once before this existed.
        """
        return {"student": cls.MEMBER, "instructor": cls.MANAGER}.get(
            str(value).lower()
        )


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
    # OpenAI Responses API (/v1/responses) — Codex คุยด้วย protocol นี้เท่านั้น
    # backend ส่วนใหญ่ยังพูดแค่ chat completions เกตเวย์จึงแปลให้ เหมือนที่ทำกับ Anthropic
    responses: bool = False


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


class ManagedBy(BaseModel):
    """Where this backend came from, when a deploy tool produced it.

    Optional: everything works with this absent, and the gateway contacts the
    deploy tool only when an administrator has connected one *and* an operator
    presses the button. Its first job is still to let a finding name the exact
    command to run instead of `./<controller>.sh` - the difference between
    advice someone can paste and advice they have to translate.

    `node` and `controller` describe the machine for a human. `lmds_node` and
    `lmds_slug` are what LMDS's own API needs, and they are separate fields on
    purpose: `node` is an ssh target, while LMDS addresses machines by the name
    in its registry, and the two are often not the same string. Guessing one
    from the other would send a restart to the wrong machine.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = "lmds"
    node: str = ""        # ssh target, e.g. neronain@100.80.132.102
    controller: str = ""  # path to the controller script on that node
    lmds_node: str = ""   # the machine's name in the LMDS node registry
    lmds_slug: str = ""   # the bundle slug LMDS knows this model by


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
    managed_by: ManagedBy | None = None
    enabled: bool = True

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.rstrip("/")

    @field_validator("api_key_env")
    @classmethod
    def _must_be_a_name_not_a_secret(cls, value: str) -> str:
        """ช่องนี้รับ *ชื่อ* ตัวแปรสภาพแวดล้อม ไม่ใช่ตัวคีย์

        คนวางคีย์จริงลงไปตรง ๆ เพราะช่องมันดูเหมือนช่องใส่คีย์ · ถ้าปล่อยผ่าน คีย์จะถูก
        เขียนลงไฟล์ registry ที่อยู่ใน git แล้วไม่มีใครรู้ตัว และคำขอก็จะยังไม่มีคีย์อยู่ดี
        เพราะเราเอาค่านี้ไปเปิดหา os.environ — ผลคือ 401 ที่หาสาเหตุไม่เจอ
        """
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(
                "ต้องเป็นชื่อตัวแปรสภาพแวดล้อม เช่น MINIMAX_API_KEY ไม่ใช่ตัวคีย์เอง — "
                "ตัวคีย์ตั้งไว้ในสภาพแวดล้อมของเครื่องที่รันเกตเวย์ "
                "(expects an env var NAME such as MINIMAX_API_KEY, not the key itself)"
            )
        return value


class ModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Stable public identifier. This is the ONLY name a member ever sees.
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$")
    display_name: str
    description: str = ""
    visibility: Visibility = Visibility.MEMBER
    tags: list[str] = Field(default_factory=list)


class SmallPromptRule(BaseModel):
    """งานจุกจิกให้ตัวเล็กรับไป — Claude Code ยิงพวกตั้งชื่อ session/สรุปหัวข้อตลอดเวลา

    ตัดสินจากขนาดคำขอเท่านั้น ไม่เดาเจตนา: เกณฑ์เป็นตัวเลขที่อ่านได้จากไฟล์ ตรวจซ้ำได้
    และอธิบายให้ผู้ใช้ฟังได้ว่าทำไมคำขอนี้ถึงไปโมเดลนั้น
    """

    model_config = ConfigDict(extra="forbid")

    under_tokens: int = Field(ge=1)
    # ข้ามกฎนี้เมื่อผู้ใช้ขอ output ยาว — prompt สั้นไม่ได้แปลว่างานเบาเสมอ
    max_output_tokens: int | None = Field(default=None, ge=1)
    target: str


class RoutingRules(BaseModel):
    """เลือก *โมเดล* ตามรูปร่างคำขอ — คนละชั้นกับ Router ที่เลือก *เครื่อง*

    alias ที่สมาชิกเรียกไม่เปลี่ยน สิทธิ์และโควตายังคิดจาก alias เดิมเสมอ
    (ดู PRD §6: ชื่อ repo ไม่เคยหลุดถึงสมาชิก) กฎพวกนี้จึงเป็นการตัดสินใจของแอดมิน
    แบบเดียวกับการ repoint alias ไม่ใช่ทางให้ผู้ใช้ข้ามสิทธิ์
    """

    model_config = ConfigDict(extra="forbid")

    # prompt ยาวเกิน window ของตัวนี้ → ส่งต่อแทนที่จะตอบ 400 ทิ้ง
    overflow: str | None = None
    small_prompt: SmallPromptRule | None = None
    # ไม่เหลือ endpoint ที่รับไหว → ไล่ตามลำดับนี้
    fallback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_self_reference(self) -> RoutingRules:
        # ตัว alias เองยังไม่รู้จักตรงนี้ (validator ระดับ spec) — เช็คได้แค่ว่าไม่ซ้ำกันเอง
        if len(self.fallback) != len(set(self.fallback)):
            raise ValueError("routing.fallback contains duplicates")
        return self


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
    routing: RoutingRules = Field(default_factory=RoutingRules)
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
        if self.protocols.responses and not any(
            e.protocols.responses or e.protocols.openai for e in enabled
        ):
            raise ValueError(
                "protocols.responses=true requires an enabled endpoint speaking either "
                "the Responses API (native passthrough) or the OpenAI API (translated)"
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

    # The former identifier is still accepted: a registry written before the
    # rename must keep loading without anyone editing every file.
    apiVersion: Literal["litegate.dev/v1", "edullm.gateway/v1"] = "litegate.dev/v1"
    kind: Literal["Model"] = "Model"
    metadata: ModelMetadata
    spec: ModelSpec

    @property
    def alias(self) -> str:
        return self.metadata.alias


class QuotaDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: Literal["day", "month", "term"] = "day"
    # Months a "term" begins on, for organisations that bill or budget on a
    # calendar of their own: [1, 4, 7, 10] for fiscal quarters, [9, 2] for a
    # two-semester year. Default is the Thai academic year this first ran on.
    term_start_months: list[int] = Field(default_factory=lambda: [1, 6, 8])
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

    # The former identifier is still accepted: a registry written before the
    # rename must keep loading without anyone editing every file.
    apiVersion: Literal["litegate.dev/v1", "edullm.gateway/v1"] = "litegate.dev/v1"
    kind: Literal["GatewayConfig"] = "GatewayConfig"
    vision_policy: VisionPolicy = Field(default_factory=VisionPolicy)
    privacy: PrivacyPolicy = Field(default_factory=PrivacyPolicy)
    quota_defaults: QuotaDefaults = Field(default_factory=QuotaDefaults)
    rate_limit_defaults: RateLimitDefaults = Field(default_factory=RateLimitDefaults)
    # Whether belonging to a workspace decides which models you may call.
    #
    # Off, membership is bookkeeping: it records who is in which class and
    # grants nothing, which is what every deployment before v1.5 did. Turning it
    # on re-permissions keys that are already in circulation, so a site with
    # live keys should run scripts/access_change_report.py first and only then
    # flip it. New deployments want it on - it is what everyone assumes it does.
    membership_grants_models: bool = True
    health_check_interval_seconds: int = Field(default=15, ge=5)
    health_check_timeout_seconds: float = Field(default=5.0, ge=0.5)
    unhealthy_threshold: int = Field(default=3, ge=1)
    healthy_threshold: int = Field(default=2, ge=1)
