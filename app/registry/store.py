"""Registry loading, validation and hot reload.

The registry is the single source of truth for *what models exist*. It is loaded
from YAML so that adding a model never requires a gateway code change (PRD §2).
A reload is atomic: if any file fails validation the previous good snapshot is
kept and the error is surfaced on /health and in the logs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.registry.schema import (
    Endpoint,
    GatewayConfig,
    ModelDefinition,
    Purpose,
    Visibility,
    VisionPolicy,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistrySnapshot:
    """Immutable view of the registry. Swapped in wholesale on reload."""

    gateway: GatewayConfig
    models: dict[str, ModelDefinition]
    errors: list[str] = field(default_factory=list)
    source_mtime: float = 0.0
    # ลายนิ้วมือของ *ชุดไฟล์* ไม่ใช่แค่เวลาแก้ล่าสุด — ลบไฟล์ที่ใหม่ที่สุดทิ้งทำให้
    # mtime สูงสุดลดลง การเทียบแบบ ">" จึงมองไม่เห็นการลบเลย
    fingerprint: str = ""

    def get(self, alias: str) -> ModelDefinition | None:
        model = self.models.get(alias)
        if model is None or not model.spec.enabled:
            return None
        return model

    def vision_policy_for(self, model: ModelDefinition) -> VisionPolicy:
        """Per-model override wins over the gateway-wide policy."""
        return model.spec.vision_policy or self.gateway.vision_policy

    def visible_to(self, role: str) -> list[ModelDefinition]:
        allowed = {
            "member": {Visibility.MEMBER},
            "manager": {Visibility.MEMBER, Visibility.MANAGER},
            "admin": {Visibility.MEMBER, Visibility.MANAGER, Visibility.ADMIN},
        }.get(role, {Visibility.MEMBER})
        return [
            m
            for m in self.models.values()
            if m.spec.enabled and m.metadata.visibility in allowed
        ]

    def by_purpose(self, role: str) -> dict[Purpose, list[ModelDefinition]]:
        grouped: dict[Purpose, list[ModelDefinition]] = {}
        for model in self.visible_to(role):
            for purpose in model.spec.purpose:
                grouped.setdefault(purpose, []).append(model)
        return grouped


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a YAML mapping at the document root")
    return data


def load_snapshot(config_dir: Path) -> RegistrySnapshot:
    """Parse gateway.yaml + models/*.yaml into a validated snapshot."""
    errors: list[str] = []
    newest_mtime = 0.0

    gateway_path = config_dir / "gateway.yaml"
    gateway = GatewayConfig()
    if gateway_path.exists():
        newest_mtime = max(newest_mtime, gateway_path.stat().st_mtime)
        try:
            gateway = GatewayConfig.model_validate(_load_yaml(gateway_path))
        except (ValidationError, ValueError) as exc:
            errors.append(f"gateway.yaml: {exc}")
    else:
        errors.append(f"gateway.yaml not found in {config_dir}, using built-in defaults")

    models: dict[str, ModelDefinition] = {}
    models_dir = config_dir / "models"
    if models_dir.is_dir():
        for path in sorted(models_dir.glob("*.y*ml")):
            newest_mtime = max(newest_mtime, path.stat().st_mtime)
            try:
                definition = ModelDefinition.model_validate(_load_yaml(path))
            except (ValidationError, ValueError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if definition.alias in models:
                errors.append(f"{path.name}: duplicate alias '{definition.alias}'")
                continue
            models[definition.alias] = definition
    else:
        errors.append(f"models directory not found: {models_dir}")

    # กฎ routing ชี้ข้าม alias จึงตรวจได้ที่นี่ที่เดียว ไม่ใช่ใน ModelSpec ที่เห็นแค่ตัวเอง
    from app.core.rules import validate_routing

    errors.extend(validate_routing(models))

    return RegistrySnapshot(
        gateway=gateway, models=models, errors=errors, source_mtime=newest_mtime,
        fingerprint=fingerprint(config_dir),
    )


def fingerprint(config_dir: Path) -> str:
    """สถานะของไฟล์ทะเบียนแบบราคาถูก — stat อย่างเดียว ไม่ parse YAML

    ใช้ตอบคำถามเดียว: "ของบนดิสก์ยังเป็นชุดเดิมกับที่โหลดไว้ไหม" ซึ่งต้องจับได้ทั้ง
    แก้ไฟล์ เพิ่มไฟล์ และ *ลบ* ไฟล์
    """
    parts: list[str] = []
    for path in (config_dir / "gateway.yaml", *sorted((config_dir / "models").glob("*.y*ml"))):
        try:
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


class RegistryStore:
    """Holds the current snapshot and refreshes it on an interval."""

    def __init__(self, config_dir: Path, reload_seconds: int = 30) -> None:
        self._config_dir = config_dir
        self._reload_seconds = reload_seconds
        self._snapshot = RegistrySnapshot(gateway=GatewayConfig(), models={})
        self._task: asyncio.Task | None = None

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    def refresh_if_stale(self) -> RegistrySnapshot:
        """โหลดใหม่เมื่อไฟล์บนดิสก์ไม่ตรงกับที่ถืออยู่ — ราคาแค่ stat ไม่กี่ครั้ง

        เกตเวย์รันหลาย worker และแต่ละตัวถือ snapshot ของตัวเอง · การแก้ทะเบียนผ่าน
        คอนโซลลงไปที่ worker เดียว ตัวอื่นจึงยังตอบด้วยของเก่าไปอีกจนกว่ารอบ reload
        จะมาถึง — คอนโซลที่ยิงซ้ำแล้วโดน worker คนละตัวจะเห็นผลไม่ตรงกัน และคำสั่งเขียน
        ที่ยืนอยู่บนของเก่าเคย *เขียนไฟล์ที่เพิ่งถูกลบกลับคืนมา*
        """
        if fingerprint(self._config_dir) != self._snapshot.fingerprint:
            return self.reload()
        return self._snapshot

    def reload(self) -> RegistrySnapshot:
        """Load from disk. Keeps the previous snapshot if the new one has no models."""
        candidate = load_snapshot(self._config_dir)
        if not candidate.models and self._snapshot.models:
            log.error(
                "registry reload produced zero models, keeping previous snapshot: %s",
                candidate.errors,
            )
            return self._snapshot
        for err in candidate.errors:
            log.error("registry: %s", err)
        self._snapshot = candidate
        log.info(
            "registry loaded: %d model(s) [%s]",
            len(candidate.models),
            ", ".join(sorted(candidate.models)),
        )
        return candidate

    async def start(self) -> None:
        self.reload()
        if self._reload_seconds > 0:
            self._task = asyncio.create_task(self._watch(), name="registry-reload")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self._reload_seconds)
            try:
                if fingerprint(self._config_dir) != self._snapshot.fingerprint:
                    log.info("registry change detected, reloading")
                    self.reload()
            except Exception:  # never let the watcher die
                log.exception("registry watch iteration failed")


def endpoint_key(alias: str, endpoint: Endpoint) -> str:
    """Stable identifier used by health tracking and usage logs."""
    return f"{alias}:{endpoint.name}"
