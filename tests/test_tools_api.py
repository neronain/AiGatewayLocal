"""Admin client-tools panel: listing merges mirror state, download is allowlisted.

These build the app against a throwaway ``tools_dir`` + registry (never the real
``data/tools``), so the security-relevant paths — allowlist, traversal, the
unpublished gate, and auth — are exercised on data we control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ASSET_NAME = "toolx-1.0.0-linux-x86_64.tar.gz"
ASSET_BYTES = b"toolx-binary-payload\x00\x01\x02"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@pytest.fixture
def tools_env(temp_db, monkeypatch, tmp_path):
    """A throwaway tools_dir + registry, wired in before the app is built.

    Mirrors the ``writable_config`` discipline: must be requested BEFORE `client`
    so the override is in place when Settings is captured at startup.
    """
    from app import config as config_mod

    tools_dir = tmp_path / "tools"
    registry_file = tmp_path / "tools.yaml"

    # Curated registry: two tools, only one of which is mirrored/published.
    registry = {
        "apiVersion": "tools.litegate/v1",
        "kind": "ClientToolRegistry",
        "tools": [
            {
                "slug": "toolx",
                "name": "Tool X",
                "summary": "A published tool.",
                "repo": "acme/toolx",
                "homepage": "https://toolx.example",
                "license": {"spdx": "MIT", "files": ["LICENSE"]},
                "verify": {"method": "sha256sums", "checksums_asset": "checksums.txt"},
                "assets": [
                    {
                        "platform": "linux",
                        "arch": "x86_64",
                        "file": "toolx-{version}-linux-x86_64.tar.gz",
                        "kind": "archive",
                    }
                ],
            },
            {
                "slug": "tooly",
                "name": "Tool Y",
                "summary": "Never mirrored yet.",
                "repo": "acme/tooly",
                "license": {"spdx": "Apache-2.0"},
                "verify": {"method": "sha256sums", "checksums_asset": "checksums.txt"},
                # ประกาศ asset ไว้แต่ยังไม่เคยมิเรอร์ — "ยังไม่ published" มาจาก
                # สถานะในโฟลเดอร์มิเรอร์ ไม่ใช่จากการไม่ประกาศ asset
                "assets": [{"platform": "linux", "file": "tooly-{version}.tar.gz"}],
            },
        ],
    }
    registry_file.write_text(yaml.safe_dump(registry), encoding="utf-8")

    # Published mirror state for toolx only.
    version = "1.0.0"
    _write_json(
        tools_dir / "toolx" / "state.json",
        {"slug": "toolx", "published_version": version, "promoted_by": "test"},
    )
    (tools_dir / "toolx" / version).mkdir(parents=True, exist_ok=True)
    (tools_dir / "toolx" / version / ASSET_NAME).write_bytes(ASSET_BYTES)
    _write_json(
        tools_dir / "toolx" / version / "manifest.json",
        {
            "slug": "toolx",
            "name": "Tool X",
            "repo": "acme/toolx",
            "tag": "v1.0.0",
            "version": version,
            "published_at": "2026-08-01T00:00:00Z",
            "status": "published",
            "verify_method": "sha256sums",
            "license": {"spdx": "MIT", "files": ["UPSTREAM-LICENSE"]},
            "assets": [
                {
                    "name": ASSET_NAME,
                    "platform": "linux",
                    "arch": "x86_64",
                    "kind": "archive",
                    "signed": False,
                    "size": len(ASSET_BYTES),
                    "sha256": "deadbeef",
                    "verified": True,
                    "verify": "sha256",
                },
                {
                    # Recorded but never fetched (missing from the release): no
                    # size, so it must not be advertised or served.
                    "name": "toolx-1.0.0-windows-x86_64.zip",
                    "platform": "windows",
                    "arch": "x86_64",
                    "kind": "archive",
                    "signed": False,
                    "size": None,
                    "verified": False,
                },
            ],
        },
    )

    monkeypatch.setenv("GW_TOOLS_DIR", str(tools_dir))
    monkeypatch.setenv("GW_TOOLS_REGISTRY_FILE", str(registry_file))
    config_mod.get_settings.cache_clear()
    yield tools_dir
    config_mod.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_list_merges_published_and_unpublished(tools_env, client):
    resp = client.get("/admin/tools", headers=auth(client.admin_key))
    assert resp.status_code == 200
    tools = {t["slug"]: t for t in resp.json()["tools"]}
    assert set(tools) == {"toolx", "tooly"}

    x = tools["toolx"]
    assert x["published"] == "1.0.0"
    assert x["published_at"] == "2026-08-01T00:00:00Z"
    assert x["license"]["spdx"] == "MIT"
    assert x["verify"]["method"] == "sha256sums"
    assert x["summary"] == "A published tool."
    assert x["repo"] == "acme/toolx"
    assert x["homepage"] == "https://toolx.example"

    by_name = {a["name"]: a for a in x["assets"]}
    served = by_name[ASSET_NAME]
    assert served["platform"] == "linux"
    assert served["size"] == len(ASSET_BYTES)
    assert served["verified"] is True
    assert served["download"] == f"/admin/tools/toolx/download?name={ASSET_NAME}"
    # The unfetched asset is listed but carries no download link.
    assert by_name["toolx-1.0.0-windows-x86_64.zip"]["download"] is None


def test_list_shows_unpublished_tool_with_nulls(tools_env, client):
    resp = client.get("/admin/tools", headers=auth(client.admin_key))
    tools = {t["slug"]: t for t in resp.json()["tools"]}
    y = tools["tooly"]
    assert y["published"] is None
    assert y["published_at"] is None
    assert y["assets"] == []
    assert y["license"]["spdx"] == "Apache-2.0"


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def test_download_returns_asset_bytes(tools_env, client):
    resp = client.get(
        f"/admin/tools/toolx/download?name={ASSET_NAME}", headers=auth(client.admin_key)
    )
    assert resp.status_code == 200
    assert resp.content == ASSET_BYTES
    assert resp.headers["content-type"] == "application/octet-stream"
    assert ASSET_NAME in resp.headers.get("content-disposition", "")


def test_download_rejects_name_not_in_manifest(tools_env, client):
    resp = client.get(
        "/admin/tools/toolx/download?name=UPSTREAM-LICENSE",
        headers=auth(client.admin_key),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_download_rejects_unfetched_asset(tools_env, client):
    # Present in the manifest but size is None → not downloadable.
    resp = client.get(
        "/admin/tools/toolx/download?name=toolx-1.0.0-windows-x86_64.zip",
        headers=auth(client.admin_key),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_download_rejects_path_traversal(tools_env, client):
    resp = client.get(
        "/admin/tools/toolx/download",
        params={"name": "../state.json"},
        headers=auth(client.admin_key),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"
    # And the manifest itself is not reachable either.
    resp2 = client.get(
        "/admin/tools/toolx/download",
        params={"name": "manifest.json"},
        headers=auth(client.admin_key),
    )
    assert resp2.status_code == 404


def test_download_unknown_tool_is_404(tools_env, client):
    resp = client.get(
        "/admin/tools/nope/download?name=whatever", headers=auth(client.admin_key)
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_download_unpublished_tool_is_400(tools_env, client):
    resp = client.get(
        "/admin/tools/tooly/download?name=whatever", headers=auth(client.admin_key)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "TOOL_NOT_PUBLISHED"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_list_requires_manager(tools_env, client, member_key):
    resp = client.get("/admin/tools", headers=auth(member_key))
    assert resp.status_code == 403


def test_download_requires_manager(tools_env, client, member_key):
    resp = client.get(
        f"/admin/tools/toolx/download?name={ASSET_NAME}", headers=auth(member_key)
    )
    assert resp.status_code == 403


def test_list_requires_auth(tools_env, client):
    resp = client.get("/admin/tools")
    assert resp.status_code == 401
