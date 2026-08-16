"""Mirror third-party client-tool releases into the gateway's own store.

Flow (deliberately ``mirror -> verify -> stage -> (human) promote``, the same
two-tier discipline the fleet uses for recipes): fetch a GitHub release, pull the
configured per-OS assets, verify each (minisign against a pinned key, or SHA-256
against the release's checksums file), fetch the upstream licence, and write a
``manifest.json`` marked ``status: "candidate"``. Nothing is offered to a
customer until :func:`promote` flips a slug's published pointer — because once we
hand a binary to a school, we are the trust anchor, so a compromised upstream
must not flow straight through.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.registry.tools_schema import ToolDefinition
from app.tools.minisign import MinisignError, verify_file

GITHUB_API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


class SyncError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def version_from_tag(tag: str) -> str:
    return tag[1:] if tag[:1].lower() == "v" else tag


def slug_dir(tools_dir: Path, slug: str) -> Path:
    return Path(tools_dir) / slug


def version_dir(tools_dir: Path, slug: str, version: str) -> Path:
    return slug_dir(tools_dir, slug) / version


# --------------------------------------------------------------------------- #
# GitHub                                                                       #
# --------------------------------------------------------------------------- #
def _headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch_latest_release(
    client: httpx.AsyncClient, repo: str, token: str = ""
) -> dict[str, Any]:
    resp = await client.get(f"{GITHUB_API}/repos/{repo}/releases/latest", headers=_headers(token))
    if resp.status_code == 404:
        raise SyncError(f"{repo}: no published release")
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise SyncError("GitHub API rate limit hit — set GW_TOOLS_GITHUB_TOKEN")
    resp.raise_for_status()
    return resp.json()


def _asset_index(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["name"]: a for a in release.get("assets", [])}


# --------------------------------------------------------------------------- #
# Verify                                                                       #
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    """Parse ``<sha256>  <filename>`` lines (the coreutils/`sha256sum` format)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


# --------------------------------------------------------------------------- #
# Download + sync one tool                                                     #
# --------------------------------------------------------------------------- #
async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    async with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            async for chunk in resp.aiter_bytes(1 << 20):
                fh.write(chunk)
    tmp.replace(dest)


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    resp = await client.get(url, follow_redirects=True)
    if resp.status_code == 200:
        return resp.text
    return None


async def sync_tool(
    tool: ToolDefinition,
    *,
    tools_dir: Path,
    client: httpx.AsyncClient,
    platforms: set[str] | None = None,
    metadata_only: bool = False,
    token: str = "",
) -> dict[str, Any]:
    """Mirror one tool's latest release. Returns the written (candidate) manifest.

    ``metadata_only`` checks the release and configured assets exist without
    downloading — a fast live check that touches the API but not 90 MB AppImages.
    """
    release = await fetch_latest_release(client, tool.repo, token)
    tag = release["tag_name"]
    version = version_from_tag(tag)
    index = _asset_index(release)
    dest = version_dir(tools_dir, tool.slug, version)

    warnings: list[str] = []
    checksums: dict[str, str] = {}
    if tool.verify.method == "sha256sums":
        name = tool.verify.checksums_asset or ""
        if name not in index:
            raise SyncError(f"{tool.slug}: checksums asset {name!r} missing from {tag}")
        text = None
        if not metadata_only:
            await _download(client, index[name]["browser_download_url"], dest / name)
            text = (dest / name).read_text(encoding="utf-8", errors="replace")
        else:
            text = await _fetch_text(client, index[name]["browser_download_url"])
        checksums = parse_checksums(text or "")

    assets_out: list[dict[str, Any]] = []
    for spec in tool.assets_for(platforms):
        filename = spec.filename(version=version, tag=tag)
        entry: dict[str, Any] = {
            "name": filename,
            "platform": spec.platform,
            "arch": spec.arch,
            "kind": spec.kind,
            "signed": spec.signed,
            "size": index.get(filename, {}).get("size"),
            "url": index.get(filename, {}).get("browser_download_url"),
            "sha256": None,
            "verified": None,  # True=proven, False=failed, None=no proof available
            "verify": None,
        }
        if filename not in index:
            entry["verified"] = False
            entry["error"] = "asset missing from release"
            warnings.append(f"{filename}: not in release {tag}")
            assets_out.append(entry)
            continue

        if metadata_only:
            assets_out.append(entry)
            continue

        target = dest / filename
        await _download(client, index[filename]["browser_download_url"], target)
        entry["sha256"] = _sha256(target)

        if tool.verify.method == "minisign" and spec.signed:
            sig_name = filename + ".sig"
            if sig_name not in index:
                entry["verified"] = False
                entry["error"] = "declared signed but no .sig in release"
                warnings.append(f"{filename}: marked signed but {sig_name} missing")
            else:
                await _download(client, index[sig_name]["browser_download_url"], dest / sig_name)
                try:
                    tc = verify_file(
                        target,
                        (dest / sig_name).read_text(encoding="utf-8", errors="replace"),
                        tool.verify.pubkey or "",
                        key_id_hex=tool.verify.key_id,
                    )
                    entry["verified"] = True
                    entry["verify"] = "minisign"
                    entry["trusted_comment"] = tc
                except MinisignError as exc:
                    entry["verified"] = False
                    entry["error"] = f"minisign: {exc}"
                    warnings.append(f"{filename}: signature FAILED — {exc}")
        elif tool.verify.method == "sha256sums":
            expected = checksums.get(filename)
            if not expected:
                entry["verified"] = None
                warnings.append(f"{filename}: no line in checksums file")
            elif expected == entry["sha256"]:
                entry["verified"] = True
                entry["verify"] = "sha256"
            else:
                entry["verified"] = False
                entry["error"] = "sha256 mismatch"
                warnings.append(f"{filename}: sha256 MISMATCH")
        else:
            # unsigned asset under a minisign tool (e.g. cc-switch .dmg) — we have
            # no cryptographic proof; keep it, but say so loudly.
            entry["verified"] = None
            warnings.append(f"{filename}: unsigned upstream — integrity+HTTPS only")

        assets_out.append(entry)

    # Licence text — required to redistribute MIT/Apache; fetch best-effort.
    licence_files: list[str] = []
    if not metadata_only:
        for rel in tool.license.files:
            text = await _fetch_text(client, f"{RAW}/{tool.repo}/HEAD/{rel}")
            if text is not None:
                out = dest / f"UPSTREAM-{Path(rel).name}"
                out.write_text(text, encoding="utf-8")
                licence_files.append(out.name)
            else:
                warnings.append(f"licence file {rel} not found upstream")

    manifest = {
        "slug": tool.slug,
        "name": tool.name,
        "repo": tool.repo,
        "tag": tag,
        "version": version,
        "published_at": release.get("published_at"),
        "synced_at": _now(),
        "status": "metadata" if metadata_only else "candidate",
        "verify_method": tool.verify.method,
        "license": {"spdx": tool.license.spdx, "files": licence_files},
        "assets": assets_out,
        "warnings": warnings,
    }
    if not metadata_only:
        dest.mkdir(parents=True, exist_ok=True)
        _write_json(dest / "manifest.json", manifest)
    return manifest


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Candidate / promote state                                                    #
# --------------------------------------------------------------------------- #
def load_manifest(tools_dir: Path, slug: str, version: str) -> dict[str, Any] | None:
    path = version_dir(tools_dir, slug, version) / "manifest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def blocking_failures(manifest: dict[str, Any]) -> list[str]:
    """Assets that actively FAILED verification (``verified is False``).

    Unverifiable-by-nature assets (``verified is None``) do not block; a real
    signature/checksum mismatch does.
    """
    return [a["name"] for a in manifest.get("assets", []) if a.get("verified") is False]


def promote(tools_dir: Path, slug: str, version: str, *, actor: str = "cli") -> dict[str, Any]:
    """Publish a vetted candidate. Refuses if any asset failed verification."""
    manifest = load_manifest(tools_dir, slug, version)
    if manifest is None:
        raise SyncError(f"{slug} {version}: no candidate manifest to promote")
    failures = blocking_failures(manifest)
    if failures:
        raise SyncError(f"{slug} {version}: refusing to promote, failed verify: {failures}")
    manifest["status"] = "published"
    _write_json(version_dir(tools_dir, slug, version) / "manifest.json", manifest)
    state = {
        "slug": slug,
        "published_version": version,
        "promoted_at": _now(),
        "promoted_by": actor,
    }
    _write_json(slug_dir(tools_dir, slug) / "state.json", state)
    return state


def published_version(tools_dir: Path, slug: str) -> str | None:
    path = slug_dir(tools_dir, slug) / "state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("published_version")


def list_candidates(tools_dir: Path, slug: str) -> list[str]:
    base = slug_dir(tools_dir, slug)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "manifest.json").exists())
