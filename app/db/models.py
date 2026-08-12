"""Persistence layer (PRD §12 Data Model).

Design rules encoded here:
  * Prompts, responses and images are NEVER columns. Privacy default is no-store
    (PRD §11); usage rows are metadata only.
  * The model *registry* lives in YAML, not in the DB. `models` here is a
    projection kept for foreign keys and reporting, refreshed on registry load.
  * API keys are stored as HMAC-SHA256 digests; the plaintext exists only in the
    response that created it.
  * Physical table and column names predate the rename to workspace vocabulary
    and are kept as-is. They are not part of the product surface, and renaming
    them would force every existing deployment through a migration to gain
    nothing a user can see. Where the Python attribute and the column differ,
    mapped_column states the column name explicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Member ID / staff ID from the university IdP.
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), default="member")  # member|manager|admin
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|suspended

    # Console sign-in. Separate from API keys on purpose: a key is a credential
    # for a program and belongs in a config file, a password is a credential for
    # a person and belongs in their head. Using one for the other is how people
    # end up pasting a production key into a browser.
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    # Bumped whenever the password changes, and carried inside every session
    # token, so changing a password signs every existing session out.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user")
    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class Workspace(Base, TimestampMixin):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    term: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")

    memberships: Mapped[list[Membership]] = relationship(back_populates="workspace")
    allowed_models: Mapped[list[WorkspaceModel]] = relationship(back_populates="workspace")


class Membership(Base, TimestampMixin):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("user_id", "course_id", name="uq_enrollment"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(
        "course_id", ForeignKey("courses.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="member")

    user: Mapped[User] = relationship(back_populates="memberships")
    workspace: Mapped[Workspace] = relationship(back_populates="memberships")


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # Optional binding: a key issued for one workspace can only spend that workspace's quota.
    workspace_id: Mapped[str | None] = mapped_column(
        "course_id", ForeignKey("courses.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(128), default="")
    # First 12 chars ("edu_sk_ab12"), shown in UI so a key can be identified.
    key_prefix: Mapped[str] = mapped_column(String(24), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_keys")


# --------------------------------------------------------------------------
# Model projection + permissions
# --------------------------------------------------------------------------
class ModelRecord(Base, TimestampMixin):
    """Projection of config/models/*.yaml. Source of truth stays in YAML."""

    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    alias: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    upstream_model: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[list] = mapped_column(JSON, default=list)
    context_length: Mapped[int] = mapped_column(Integer, default=0)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=0)

    supports_text: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_image: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_video: Mapped[bool] = mapped_column(Boolean, default=False)

    supports_streaming: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_agentic: Mapped[bool] = mapped_column(Boolean, default=False)

    supports_openai: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_anthropic: Mapped[bool] = mapped_column(Boolean, default=False)
    claude_code_compatible: Mapped[bool] = mapped_column(Boolean, default=False)

    visibility: Mapped[str] = mapped_column(String(32), default="member")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    compatibility: Mapped[list[ModelCompatibility]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )


class WorkspaceModel(Base, TimestampMixin):
    """Which aliases a workspace may call. Absent row == not permitted."""

    __tablename__ = "course_models"
    __table_args__ = (UniqueConstraint("course_id", "model_alias", name="uq_course_model"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        "course_id", ForeignKey("courses.id"), index=True
    )
    model_alias: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    workspace: Mapped[Workspace] = relationship(back_populates="allowed_models")


class ModelCompatibility(Base):
    """Result of the MODEL-001..010 suite (PRD §18). Never inferred from names."""

    __tablename__ = "model_compatibility"
    __table_args__ = (UniqueConstraint("model_id", "feature", name="uq_model_feature"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id"), index=True)
    feature: Mapped[str] = mapped_column(String(64))  # chat|streaming|vision|tools|claude_code
    status: Mapped[str] = mapped_column(String(32), default="not_tested")
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    test_version: Mapped[str] = mapped_column(String(32), default="")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, default="")

    model: Mapped[ModelRecord] = relationship(back_populates="compatibility")


# --------------------------------------------------------------------------
# Quota + usage
# --------------------------------------------------------------------------
class QuotaPolicy(Base, TimestampMixin):
    """Most specific matching policy wins: user > workspace > global."""

    __tablename__ = "quota_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(16), default="global")  # global|workspace|user
    workspace_id: Mapped[str | None] = mapped_column(
        "course_id", ForeignKey("courses.id"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    model_alias: Mapped[str | None] = mapped_column(String(64), index=True)

    window: Mapped[str] = mapped_column(String(16), default="day")  # day|month|term
    max_requests: Mapped[int] = mapped_column(Integer, default=0)  # 0 == unlimited
    max_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    max_images: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class QuotaCounter(Base):
    """Consumption per (subject, window). Redis is the hot path; this is durable."""

    __tablename__ = "quota_counters"
    __table_args__ = (
        UniqueConstraint("subject_key", "window_start", name="uq_counter_window"),
        Index("ix_counter_window", "window_start", "window_end"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    subject_key: Mapped[str] = mapped_column(String(160), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    requests: Mapped[int] = mapped_column(Integer, default=0)
    text_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    visual_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UsageLog(Base):
    """One row per completed request. Metadata only - no prompt, no image."""

    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_ts_user", "ts", "user_id"),
        Index("ix_usage_ts_workspace", "ts", "course_id"),
        Index("ix_usage_ts_model", "ts", "model_alias"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    workspace_id: Mapped[str | None] = mapped_column("course_id", String(32), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), index=True)

    model_alias: Mapped[str] = mapped_column(String(64), index=True)
    endpoint_name: Mapped[str] = mapped_column(String(64), default="")
    protocol: Mapped[str] = mapped_column(String(16), default="openai")  # openai|anthropic
    request_modality: Mapped[str] = mapped_column(String(32), default="text")  # text|text+image
    stream: Mapped[bool] = mapped_column(Boolean, default=False)

    text_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    visual_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    # "upstream" when the backend reported usage; "estimated" when we derived it.
    token_accounting: Mapped[str] = mapped_column(String(16), default="upstream")

    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="success")  # success|error|aborted
    http_status: Mapped[int] = mapped_column(Integer, default=200)
    error_code: Mapped[str | None] = mapped_column(String(64), index=True)
    client_agent: Mapped[str] = mapped_column(String(128), default="")
    cost_units: Mapped[float] = mapped_column(Float, default=0.0)


class ModelTestRun(Base):
    """A console-triggered MODEL-001..010 run.

    Stored in the database rather than in process memory so that polling for
    progress works when the poll lands on a different uvicorn worker than the
    one executing the run.
    """

    __tablename__ = "model_test_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    model_alias: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_user_id: Mapped[str | None] = mapped_column(String(32))
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    """Admin-plane mutations. Retained longer than usage."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
