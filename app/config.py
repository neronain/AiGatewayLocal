"""Gateway process settings, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict



# ── ลายเซ็นผู้สร้าง ────────────────────────────────────────────────────────────
#
# รวมไว้ที่เดียวเพราะข้อความที่ก๊อปกระจายหลายไฟล์จะแก้ไม่ทั่ว แล้วบางจุดหายไปเงียบ ๆ
#
# เคสจริง 2026-08-20: มีคนเอา repo ไปพัฒนาต่อโดยไม่ให้เครดิต · LiteGate เป็น MIT ซึ่ง
# *อนุญาตให้ fork ได้* แต่ **บังคับให้คงประกาศลิขสิทธิ์ไว้** — การถอดออกคือผิดสัญญาอนุญาต
# ไม่ใช่แค่เรื่องมารยาท · ลายเซ็นจึงต้องอยู่ในที่ที่ผู้ใช้ปลายทางเห็น (footer, header ของ
# ทุก response, /healthz) ไม่ใช่แค่ใน README ที่ลบทิ้งได้ในบรรทัดเดียว
AUTHOR = "neronain"
AUTHOR_URL = "https://www.facebook.com/neronain.minidev"
PRODUCT = "LiteGate"
LICENSE_NOTE = "MIT — attribution required"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    workers: int = 4

    # Security
    api_key_pepper: str = "dev-only-insecure-pepper"
    # ตั้งค่านี้ = เก็บสำเนาของ API key ที่ปิดผนึกไว้ เพื่อให้ผู้ดูแลเรียกดูซ้ำได้
    # ว่าง (ค่าตั้งต้น) = เก็บแค่ hash เหมือนเดิม และไม่มีอะไรให้เรียกดู
    #
    # ต้องมาจาก environment ไม่ใช่ฐานข้อมูล — เก็บกุญแจไว้ในกล่องเดียวกับของที่ล็อก
    # แล้วการเข้ารหัสไม่ได้ซื้ออะไรเลย · ดู app/core/keyvault.py
    key_reveal_secret: str = ""
    # Kept as a raw string: pydantic-settings JSON-decodes complex types straight
    # from the environment, before any validator runs, so a plain
    # "a,b" - or an empty value - would fail to parse as list[str].
    cors_origins: str = ""
    bootstrap_admin_key: str = ""
    # First administrator, created on an empty database so nobody has to dig a
    # token out of the log to get in. Password is generated when unset.
    admin_user: str = "admin"
    admin_password: str = ""
    # Keys a member may hold at once. Keys are what nobody cleans up; a list
    # that grows without bound is one nobody can audit.
    max_keys_per_member: int = 5
    # Alias the console assistant talks to. Empty = pick the best chat model the
    # caller is already allowed to use, so a fresh install needs no setting.
    assistant_model: str = ""

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data/gateway.db"
    redis_url: str = ""

    # Registry
    config_dir: Path = Path("./config")
    registry_reload_seconds: int = 30

    # Client tools — third-party downloads the gateway mirrors and offers to
    # customers (cc-switch, rtk…). Definitions are curated in tools_registry_file
    # (git, vetted); mirrored binaries land in tools_dir (writable, git-ignored).
    # auto_sync stays OFF by design: we become the trust anchor once a school
    # runs what we hand them, so promotion is a conscious human step, never a pull.
    tools_dir: Path = Path("./data/tools")
    tools_registry_file: Path = Path("./config/tools.yaml")
    tools_auto_sync: bool = False
    tools_github_token: str = ""  # optional, raises the GitHub API rate limit

    # Upstream
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 600.0
    upstream_max_connections: int = 200

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def self_base_url(self) -> str:
        """How the gateway reaches its own API.

        Used by the console-triggered test suite, which drives the public API.
        It must NOT be derived from the incoming request: the browser may have
        arrived via a proxy, a port-forward or a public hostname that the server
        itself cannot resolve.
        """
        host = self.host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}

    @property
    def models_dir(self) -> Path:
        return self.config_dir / "models"

    @property
    def gateway_yaml(self) -> Path:
        return self.config_dir / "gateway.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()
