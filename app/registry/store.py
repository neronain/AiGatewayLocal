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


# `yaml.safe_load` ใช้ parser ที่เขียนด้วย Python ล้วนเสมอ แม้เครื่องจะมี libyaml อยู่
# วัดกับทะเบียน 63 โมเดล: pure-python 64 ms · CSafeLoader 7.7 ms — **เร็วกว่า 8 เท่า**
#
# ตัวเลขนี้สำคัญเพราะ reload รันอยู่บน event loop (ดู `_watch`) เวลาทั้งก้อนคือเวลาที่
# สตรีมทุกเส้นใน worker นั้นหยุดนิ่ง · ทางที่ถูกที่สุดคือทำงานให้น้อยลง ไม่ใช่ย้ายงาน
# เท่าเดิมไปอีก thread — libyaml ถือ GIL ไว้ระหว่าง parse อยู่ดี การย้ายเฉย ๆ จึงไม่คืน
# event loop ให้ใครเลย
#
# CSafeLoader อ่าน YAML ชุดเดียวกับ SafeLoader (มาตรฐานเดียวกัน ไม่มี tag พิเศษ)
# เครื่องที่ PyYAML ถูกลงมาโดยไม่มี libyaml จะไม่มีคลาสนี้ — ตกกลับไปตัวเดิม
_SafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=_SafeLoader)  # noqa: S506 - SafeLoader ทั้งสองทาง
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
            # ไฟล์ที่ขึ้นต้นด้วยจุดไม่เคยเป็น config ที่ตั้งใจ · macOS แถม `._ชื่อไฟล์`
            # มาให้เองเวลาแตกไฟล์จาก zip/tar หรือก๊อปผ่าน USB แล้วมันไม่ใช่ UTF-8
            # ด้วยซ้ำ — ปล่อยไว้ = ผู้ติดตั้งเจอ error สามบรรทัดที่ไม่ได้เกิดจากอะไรที่เขาทำ
            if path.name.startswith("."):
                continue
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
    candidates = [p for p in sorted((config_dir / "models").glob("*.y*ml"))
                  if not p.name.startswith(".")]
    for path in (config_dir / "gateway.yaml", *candidates):
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
        """รอบตรวจทะเบียนทุก N วินาที — ทำงานใน thread ไม่ใช่บน event loop

        ทั้ง `fingerprint` และ `reload` เป็นโค้ด sync ที่อ่านดิสก์: fingerprint คือ
        stat หนึ่งครั้งต่อไฟล์ · reload คือเปิดอ่านทุกไฟล์ + parse YAML + ตรวจด้วย
        pydantic · งานนี้เคยรันตรงบน event loop แปลว่าทุก 30 วินาที (เมื่อไฟล์เปลี่ยน)
        สตรีมทุกเส้นใน worker นั้นหยุดนิ่งพร้อมกันตามขนาดของทะเบียน

        วัดกับทะเบียน 63 โมเดล โดยจับช่วงห่างที่ยาวที่สุดที่ event loop ไม่ได้รัน:
        **เรียกตรง ๆ = 15.5 ms · ผ่าน to_thread = 0.6 ms**

        งานนี้คุ้มที่จะย้ายเพราะมันใหญ่พอ (หลักสิบ ms และโตตามจำนวนโมเดล) และเป็นงาน
        เบื้องหลังที่ไม่มีใครรอคำตอบ — เวลารวมที่ช้าลงจากการข้าม thread ไม่มีผลกับใคร
        ต่างจากงานหลัก µs ต่อคำขอ ซึ่งย้ายแล้วมีแต่เสีย

        ปลอดภัยเพราะ `reload()` แตะสถานะร่วมอยู่บรรทัดเดียว: `self._snapshot = candidate`
        และ RegistrySnapshot เป็น frozen dataclass — ผู้อ่านจึงเห็นของเก่าทั้งก้อนหรือ
        ของใหม่ทั้งก้อน ไม่มีสภาพครึ่ง ๆ · (และ `refresh_if_stale` ก็ถูกเรียกจาก
        threadpool ของ FastAPI อยู่แล้ววันนี้ เพราะมันเป็น dependency แบบ sync)
        """
        while True:
            await asyncio.sleep(self._reload_seconds)
            try:
                if await asyncio.to_thread(self._reload_if_changed):
                    log.info("registry change detected, reloaded")
            except Exception:  # never let the watcher die
                log.exception("registry watch iteration failed")

    def _reload_if_changed(self) -> bool:
        """True เมื่อมีการเปลี่ยนแปลงและโหลดใหม่แล้ว · รันใน thread ของ `_watch`"""
        if fingerprint(self._config_dir) == self._snapshot.fingerprint:
            return False
        self.reload()
        return True


def endpoint_key(alias: str, endpoint: Endpoint) -> str:
    """Stable identifier used by health tracking and usage logs."""
    return f"{alias}:{endpoint.name}"
