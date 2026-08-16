"""Admin plane: the client-tools mirror, surfaced for the console panel.

The gateway mirrors third-party client tools (rtk, cc-switch…) through a
``mirror -> verify -> stage -> promote`` pipeline (see :mod:`app.tools.sync`).
This router is the read side of that store: it merges the curated registry
(``config/tools.yaml``, what we are *willing* to offer) with each slug's
published mirror state (what is actually on disk and vetted), and it serves the
published binaries back — but only ones that appear in a published manifest, so
a caller can never walk the filesystem through the ``name`` parameter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.core.auth import Principal, require_manager
from app.core.errors import ErrorCode, GatewayError
from app.registry.tools_schema import ToolDefinition, load_tool_registry
from app.state import AppState, get_state
from app.tools.sync import load_manifest, published_version, version_dir

router = APIRouter(prefix="/admin/tools", tags=["admin", "tools"])


def _download_path(slug: str, name: str) -> str:
    return f"/admin/tools/{slug}/download?name={name}"


def _asset_view(slug: str, asset: dict[str, Any]) -> dict[str, Any]:
    """The per-asset shape the console renders (a subset of the manifest entry)."""
    name = asset.get("name")
    # An asset is downloadable only if the mirror actually pulled a file for it;
    # `size is None` marks an entry that was recorded but never fetched.
    downloadable = bool(name) and asset.get("size") is not None
    return {
        "name": name,
        "platform": asset.get("platform"),
        "arch": asset.get("arch"),
        "kind": asset.get("kind"),
        "verified": asset.get("verified"),
        "size": asset.get("size"),
        "download": _download_path(slug, name) if downloadable else None,
    }


def _tool_view(tool: ToolDefinition, tools_dir: Path) -> dict[str, Any]:
    """Merge one registry entry with its mirror state for the listing."""
    view: dict[str, Any] = {
        "slug": tool.slug,
        "name": tool.name,
        "summary": tool.summary,
        "repo": tool.repo,
        "homepage": tool.homepage,
        "license": {"spdx": tool.license.spdx},
        "verify": {"method": tool.verify.method},
        "published": None,
        "published_at": None,
        "assets": [],
    }

    version = published_version(tools_dir, tool.slug)
    if version is None:
        return view
    manifest = load_manifest(tools_dir, tool.slug, version)
    # A dangling state.json (pointer without a manifest) is treated as "not
    # published" rather than a 500 — the console still lists the tool.
    if manifest is None or manifest.get("status") != "published":
        return view

    view["published"] = version
    view["published_at"] = manifest.get("published_at")
    view["assets"] = [_asset_view(tool.slug, a) for a in manifest.get("assets", [])]
    return view


@router.get("")
async def list_tools(
    actor: Principal = Depends(require_manager),
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """Every curated tool, merged with its published mirror state.

    Tools with no published version yet are still listed (``published: null``,
    empty ``assets``) so the console can show the whole catalogue.
    """
    settings = state.settings
    registry = load_tool_registry(settings.tools_registry_file)
    tools = [_tool_view(tool, settings.tools_dir) for tool in registry.tools]
    return {"tools": tools}


@router.get("/{slug}/download")
async def download_tool_asset(
    slug: str,
    name: str = Query(..., description="Asset filename from the published manifest"),
    actor: Principal = Depends(require_manager),
    state: AppState = Depends(get_state),
) -> FileResponse:
    """Serve one published asset by its exact manifest name.

    The ``name`` is an allowlist lookup against the published manifest, never a
    path joined onto disk: ``Path(name).name == name`` rejects any separator or
    ``..`` before we even consult the manifest, and only a name that is present
    in a *published* manifest resolves to a file.
    """
    settings = state.settings
    registry = load_tool_registry(settings.tools_registry_file)
    tool = registry.get(slug)
    if tool is None:
        raise GatewayError(ErrorCode.TOOL_NOT_FOUND, f"Unknown tool '{slug}'.")

    version = published_version(settings.tools_dir, slug)
    if version is None:
        raise GatewayError(
            ErrorCode.TOOL_NOT_PUBLISHED,
            f"Tool '{slug}' has no published version to download.",
        )
    manifest = load_manifest(settings.tools_dir, slug, version)
    if manifest is None or manifest.get("status") != "published":
        raise GatewayError(
            ErrorCode.TOOL_NOT_PUBLISHED,
            f"Tool '{slug}' has no published version to download.",
        )

    # Reject traversal (`..`, separators) before trusting the name at all.
    if Path(name).name != name:
        raise GatewayError(ErrorCode.TOOL_NOT_FOUND, f"No asset named '{name}'.")

    allowed = {
        a["name"]
        for a in manifest.get("assets", [])
        if a.get("name") and a.get("size") is not None
    }
    if name not in allowed:
        raise GatewayError(
            ErrorCode.TOOL_NOT_FOUND,
            f"'{name}' is not a downloadable asset of '{slug}' {version}.",
        )

    target = version_dir(settings.tools_dir, slug, version) / name
    if not target.is_file():
        raise GatewayError(
            ErrorCode.TOOL_NOT_FOUND,
            f"Asset '{name}' is listed but missing from the mirror.",
        )

    return FileResponse(
        target, media_type="application/octet-stream", filename=name
    )
