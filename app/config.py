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
