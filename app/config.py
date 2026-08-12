"""Gateway process settings, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data/gateway.db"
    redis_url: str = ""

    # Registry
    config_dir: Path = Path("./config")
    registry_reload_seconds: int = 30

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
