"""Schema for the curated client-tools registry (``config/tools.yaml``).

This registry is *curated* config, committed to git and vetted by us — it lists
the third-party client tools the gateway is willing to mirror and offer to
customers, and pins how each one is verified. It is deliberately separate from
the mirrored *binaries* (which live under ``settings.tools_dir`` and are
git-ignored): definitions are reviewed, binaries are fetched.

Mirrors the shape of ``app/registry/schema.py`` (an ``apiVersion``/``kind``
envelope over pydantic models) so it reads like the model registry beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

VerifyMethod = Literal["minisign", "sha256sums"]


class LicenseSpec(BaseModel):
    """The upstream licence — both to record it and to satisfy redistribution.

    ``files`` are paths in the upstream repo (e.g. ``LICENSE``, ``NOTICE``) that
    the sync fetches and stores next to the binaries. MIT and Apache-2.0 both
    require shipping the licence text with any redistributed copy.
    """

    spdx: str
    files: list[str] = Field(default_factory=list)


class VerifySpec(BaseModel):
    """How a downloaded asset is proven trustworthy before we offer it.

    - ``minisign`` proves **authenticity** against ``pubkey`` (pinned here);
      strongest, but only covers assets that actually ship a ``.sig``.
    - ``sha256sums`` proves **integrity** against a checksums file in the same
      release. The checksums file itself is unsigned, so this guards against a
      corrupted download, not a compromised release — we lean on HTTPS + the
      gated human promote for authenticity in that case.
    """

    method: VerifyMethod
    pubkey: str | None = None  # bare base64 minisign public-key payload
    key_id: str | None = None  # 8-byte key id, hex (optional cross-check)
    checksums_asset: str | None = None  # e.g. "checksums.txt"

    @model_validator(mode="after")
    def _requirements(self) -> VerifySpec:
        if self.method == "minisign" and not self.pubkey:
            raise ValueError("verify.method 'minisign' requires a pinned pubkey")
        if self.method == "sha256sums" and not self.checksums_asset:
            raise ValueError("verify.method 'sha256sums' requires checksums_asset")
        return self


class AssetSpec(BaseModel):
    """One downloadable artefact for one platform/arch.

    ``file`` is a template expanded with ``{version}`` (tag without a leading
    ``v``) and ``{tag}``; assets whose names carry no version (rtk's tarballs)
    just contain no placeholder. ``signed`` marks that a matching ``{file}.sig``
    exists — some releases sign only *some* assets (cc-switch's .dmg is not
    signed), so this is per-asset, not per-tool.
    """

    platform: Literal["windows", "macos", "linux"]
    arch: Literal["x86_64", "arm64", "universal"] = "x86_64"
    file: str
    kind: Literal["installer", "portable", "archive", "package"] = "installer"
    signed: bool = False

    def filename(self, *, version: str, tag: str) -> str:
        return self.file.format(version=version, tag=tag)


class ToolDefinition(BaseModel):
    slug: str
    name: str
    summary: str = ""
    repo: str  # "owner/name" on GitHub
    homepage: str | None = None
    license: LicenseSpec
    verify: VerifySpec
    assets: list[AssetSpec] = Field(default_factory=list)
    # Optional preset wiring (used in a later phase): how this tool is pointed at
    # our gateway (base URL + a minted scoped key). Kept freeform so adding the
    # injector later needs no schema change.
    preset: dict | None = None

    @model_validator(mode="after")
    def _consistency(self) -> ToolDefinition:
        if "/" not in self.repo:
            raise ValueError(f"repo must be 'owner/name', got {self.repo!r}")
        if self.verify.method == "minisign":
            signed = [a for a in self.assets if a.signed]
            if not signed:
                raise ValueError(
                    f"{self.slug}: verify is minisign but no asset is marked signed"
                )
        return self

    def assets_for(self, platforms: set[str] | None) -> list[AssetSpec]:
        if not platforms:
            return list(self.assets)
        return [a for a in self.assets if a.platform in platforms]


class ToolRegistry(BaseModel):
    apiVersion: str = "tools.litegate/v1"
    kind: str = "ClientToolRegistry"
    tools: list[ToolDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_slugs(self) -> ToolRegistry:
        seen = set()
        for tool in self.tools:
            if tool.slug in seen:
                raise ValueError(f"duplicate tool slug {tool.slug!r}")
            seen.add(tool.slug)
        return self

    def get(self, slug: str) -> ToolDefinition | None:
        return next((t for t in self.tools if t.slug == slug), None)


def load_tool_registry(path: Path) -> ToolRegistry:
    """Load and validate ``config/tools.yaml``. An empty/missing file is allowed."""
    path = Path(path)
    if not path.exists():
        return ToolRegistry()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ToolRegistry.model_validate(raw)
