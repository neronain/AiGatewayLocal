"""Command line for the client-tools mirror: ``python -m app.tools <cmd>``.

    python -m app.tools list                     # registry + what's mirrored/published
    python -m app.tools sync [slug ...]          # mirror latest, verify, stage as candidate
    python -m app.tools sync --check [slug ...]   # metadata-only: is the release + assets there?
    python -m app.tools promote <slug> <version>  # publish a vetted candidate (gated)
    python -m app.tools show <slug> <version>     # print a candidate's manifest

Sync stages candidates only; nothing is offered to a customer until `promote`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

from app.config import get_settings
from app.registry.tools_schema import load_tool_registry
from app.tools import sync as S


def _registry(settings):
    return load_tool_registry(settings.tools_registry_file)


def _select(reg, slugs):
    if not slugs:
        return list(reg.tools)
    picked = []
    for slug in slugs:
        tool = reg.get(slug)
        if tool is None:
            sys.exit(f"unknown tool {slug!r}; known: {', '.join(t.slug for t in reg.tools)}")
        picked.append(tool)
    return picked


async def _sync(settings, slugs, platforms, check):
    reg = _registry(settings)
    tools = _select(reg, slugs)
    rc = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        for tool in tools:
            # ตัวที่แจกผ่าน npm/Docker ไม่มีอะไรให้ดึงมาตรวจ · ข้ามไปเงียบ ๆ
            # ไม่ใช่ error เพราะมันอยู่ในรายการโดยตั้งใจ
            if not tool.assets:
                print(f"• {tool.slug}: ไม่มีไฟล์ให้มิเรอร์ (ติดตั้งผ่าน "
                      f"{', '.join(tool.install or {})}) — ข้าม")
                continue
            try:
                manifest = await S.sync_tool(
                    tool,
                    tools_dir=settings.tools_dir,
                    client=client,
                    platforms=platforms,
                    metadata_only=check,
                    token=settings.tools_github_token,
                )
            except Exception as exc:  # noqa: BLE001 - surface, keep going
                print(f"✗ {tool.slug}: {exc}")
                rc = 1
                continue
            ok = sum(1 for a in manifest["assets"] if a.get("verified") is True)
            failed = S.blocking_failures(manifest)
            noproof = sum(1 for a in manifest["assets"] if a.get("verified") is None)
            verb = "checked" if check else "staged"
            print(f"{'✗' if failed else '•'} {tool.slug} {manifest['tag']}: {verb} "
                  f"{len(manifest['assets'])} assets — {ok} verified, {noproof} unproven, "
                  f"{len(failed)} FAILED")
            for w in manifest["warnings"]:
                print(f"    ! {w}")
            if failed:
                rc = 1
            elif not check:
                version = manifest["version"]
                print(f"    → candidate at {S.version_dir(settings.tools_dir, tool.slug, version)}")
                print(f"    → promote with:  python -m app.tools promote {tool.slug} {version}")
    return rc


def _list(settings):
    reg = _registry(settings)
    if not reg.tools:
        print("(no tools in registry)")
        return 0
    for tool in reg.tools:
        pub = S.published_version(settings.tools_dir, tool.slug)
        cands = S.list_candidates(settings.tools_dir, tool.slug)
        how = tool.verify.method if tool.verify else "ไม่มีไฟล์ให้ตรวจ"
        print(f"{tool.slug:12} {tool.license.spdx:11} verify={how:11} "
              f"published={pub or '-':10} candidates={','.join(cands) or '-'}")
        print(f"             {tool.name} — {tool.repo}")
    return 0


def _promote(settings, slug, version):
    try:
        state = S.promote(settings.tools_dir, slug, version)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"cannot promote: {exc}")
    print(f"✓ published {slug} {version} (was {state.get('promoted_at')})")
    return 0


def _show(settings, slug, version):
    manifest = S.load_manifest(settings.tools_dir, slug, version)
    if manifest is None:
        sys.exit(f"no manifest for {slug} {version}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="mirror + verify + stage as candidate")
    p_sync.add_argument("slugs", nargs="*")
    p_sync.add_argument("--platform", help="comma list: windows,macos,linux (default all)")
    p_sync.add_argument("--check", action="store_true", help="metadata only, no download")

    sub.add_parser("list", help="show registry + mirror state")

    p_prom = sub.add_parser("promote", help="publish a vetted candidate")
    p_prom.add_argument("slug")
    p_prom.add_argument("version")

    p_show = sub.add_parser("show", help="print a candidate manifest")
    p_show.add_argument("slug")
    p_show.add_argument("version")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.cmd == "sync":
        platforms = {p.strip() for p in args.platform.split(",")} if args.platform else None
        return asyncio.run(_sync(settings, args.slugs, platforms, args.check))
    if args.cmd == "list":
        return _list(settings)
    if args.cmd == "promote":
        return _promote(settings, args.slug, args.version)
    if args.cmd == "show":
        return _show(settings, args.slug, args.version)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
