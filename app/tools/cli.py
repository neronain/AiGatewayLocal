"""Command line for the client-tools mirror: ``python -m app.tools <cmd>``.

    python -m app.tools list                     # registry + what's mirrored/published
    python -m app.tools sync [slug ...]          # mirror latest, verify, stage as candidate
    python -m app.tools sync --check [slug ...]   # metadata-only: is the release + assets there?
    python -m app.tools promote <slug> <version>  # publish a vetted candidate (gated)
    python -m app.tools show <slug> <version>     # print a candidate's manifest

Sync stages candidates only; nothing is offered to a customer until `promote`.

Also here, because it is the one operator command line the gateway has: the
sealed copies of API keys (GW_KEY_REVEAL_SECRET). Run these from the install
directory as the service user, so `.env` and the database are the gateway's own.

    python -m app.tools keyvault status    # which secret each sealed copy opens under
    python -m app.tools keyvault reseal    # move copies from the previous secret to the current
                                           # (asks first; --yes for scripts)
    python -m app.tools keyvault key-id    # key id of a secret read from stdin (never echoed)

`status` and `reseal` exit 0 when nothing is left to do, 1 when copies are still
waiting for a re-seal or cannot be opened, 2 when the command was refused, 3 when
a re-seal was stopped by an error part-way (what it moved stays moved and is
recorded; run it again).
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


# ── สำเนา API key ที่ผนึกไว้ ─────────────────────────────────────────────────
#
# ทางเดียวกับปุ่มในคอนโซล (เรียก app.core.keyrotation ตัวเดียวกัน) · มีไว้สำหรับตอนที่
# เกตเวย์ปิดอยู่ หรือเมื่อคนที่ถือ secret คือคนที่เข้าเครื่องได้ ไม่ใช่คนที่เข้าคอนโซลได้

def _print_survey(found) -> None:  # noqa: ANN001
    from app.core import keyvault

    ids = ""
    if found.current_key_id:
        ids = f" · current key id {found.current_key_id}"
        if found.previous_key_id:
            ids += f" · previous key id {found.previous_key_id}"
    print(f"key reveal: {'on' if found.enabled else 'off'}{ids}")
    print(f"sealed copies: {found.sealed} - current={found.counts[keyvault.CURRENT]} "
          f"previous={found.counts[keyvault.PREVIOUS]} lost={found.counts[keyvault.LOST]} "
          f"off={found.counts[keyvault.OFF]}")
    if found.lost:
        print("cannot be opened:")
        for row in found.lost:
            why = {
                keyvault.DAMAGED: "damaged - no secret opens it",
                keyvault.NOT_THIS_KEY: "opens, but is a copy of a different key - the row "
                                       "was changed outside the gateway",
                keyvault.NEWER_FORMAT: "written in a format this version does not read",
                keyvault.WRONG_SECRET: f"sealed under key id {row['sealed_key_id']}",
            }.get(row["reason"], "sealed by 1.12.1 or earlier - the secret that sealed it "
                                 "is not recorded (or it is damaged)")
            print(f"  {row['key_prefix']}…  {row['name'] or '(unnamed)'}"
                  f"{'  [revoked]' if row['revoked'] else ''}  {why}")
    for warning in found.warnings:
        print(f"{'!' if warning['level'] != 'info' else '·'} {warning['message']}")


def _confirm_reseal(before) -> bool:  # noqa: ANN001
    """ถามก่อนย้าย พร้อมบอกว่าจะย้ายจาก secret ตัวไหนไปตัวไหน

    คำสั่งนี้ใช้ secret ของ shell ที่รันมัน ซึ่ง **ไม่จำเป็นต้องเป็นชุดเดียวกับของเกตเวย์** —
    export ค่าผิดหรือรันผิดโฟลเดอร์ แล้วสำเนาทุกใบจะถูกย้ายไปอยู่ใต้ secret ที่เกตเวย์ไม่รู้จัก
    ปุ่มในคอนโซลไม่มีปัญหานี้เพราะใช้ secret ของ process เกตเวย์เอง · ป้ายที่พิมพ์ออกมาเทียบกับ
    ที่คอนโซลแสดงได้ (แบบเดียวกับที่ scripts/restore.sh ให้พิมพ์ 'restore' ก่อนทับของจริง)
    """
    from app.core import keyvault

    pending = before.counts[keyvault.PREVIOUS]
    if not pending:
        return True                 # ไม่มีอะไรจะถูกเขียน — ไม่มีอะไรให้ถาม
    print(f"About to re-seal {pending} sealed key copies: from the secret with key id "
          f"{before.previous_key_id} to the secret with key id {before.current_key_id}.")
    print("This command uses the secrets of the shell it runs in, which need not be the "
          "gateway's.\nCheck both key ids against the console (Access & Keys > API keys) "
          "before going on.")
    try:
        reply = input("Type 'reseal' to go on: ")
    except EOFError:
        reply = ""
    return reply.strip() == "reseal"


async def _reseal_stopped(session, done, exc: Exception) -> int:  # noqa: ANN001
    """การผนึกใหม่ล้มกลางทาง: จดสิ่งที่ย้ายไปแล้ว แล้วบอกผู้ดูแลเป็นประโยค ไม่ใช่ traceback

    ข้อความของข้อผิดพลาดพิมพ์ได้: engine ซ่อนค่าที่ผูกกับ SQL แล้ว (`hide_parameters` ใน
    app/db/session.py) จึงเหลือแต่ชนิด ข้อความของไดรเวอร์ และตัวคำสั่ง
    """
    from app.core import keyrotation

    print(f"re-seal stopped by an error after moving {done.resealed} sealed key copies:\n"
          f"  {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        await session.rollback()
        await keyrotation.record_reseal_from_cli(session, done, interrupted=True)
    except Exception as failed:  # noqa: BLE001 - ฐานที่ทำให้งานล้มอาจจดไม่ได้เช่นกัน
        print(f"and the audit record could not be written ({type(failed).__name__}) - note "
              "by hand who ran this and when.", file=sys.stderr)
    print("What was moved stays moved, and every copy still opens while both secrets are "
          "set.\nRun the command again to finish.", file=sys.stderr)
    return 3


async def _keyvault(action: str, assume_yes: bool) -> int:
    from app.core import keyrotation
    from app.db.session import dispose_db, session_scope

    try:
        async with session_scope() as session:
            if action == "reseal":
                before = await keyrotation.survey(session)
                await session.commit()      # ไม่ถือ transaction ค้างระหว่างรอคนตอบ
                if before.enabled and not assume_yes and not _confirm_reseal(before):
                    print("Nothing was changed.", file=sys.stderr)
                    return 2
                done = keyrotation.ResealResult()
                try:
                    await keyrotation.reseal(session, done)
                except keyrotation.ResealRefused as exc:
                    print(f"refused: {exc}", file=sys.stderr)
                    return 2
                except Exception as exc:  # noqa: BLE001 - จดก่อน แล้วค่อยบอกผู้ดูแล
                    # ล้มกลางทาง: แถวที่ย้ายไปแล้วอยู่ถาวร (commit ทีละแถว) จึงต้องมีบันทึก
                    # เหมือนที่ route ทำ · เดิมจดหลังงานจบเท่านั้น รอบที่ล้มที่ใบที่สามจึงย้าย
                    # สองใบไปใต้ secret ตัวใหม่โดยไม่มีแถว audit เลย (ผู้ตรวจอิสระ 2026-10-09)
                    return await _reseal_stopped(session, done, exc)
                await keyrotation.record_reseal_from_cli(session, done)
                print(f"resealed {done.resealed} · already current {done.already_current} · "
                      f"cannot be opened {done.lost} · changed meanwhile {done.changed_meanwhile}")
            found = await keyrotation.survey(session)
    finally:
        await dispose_db()
    _print_survey(found)
    return 1 if found.needs_attention else 0


def _key_id() -> int:
    """ป้ายของ secret ที่อ่านจาก stdin — ไม่รับเป็นอาร์กิวเมนต์ เพราะจะค้างใน history ของ shell"""
    import getpass

    from app.core import keyvault

    secret = (getpass.getpass("secret: ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not secret:
        print("no secret given on stdin", file=sys.stderr)
        return 2
    print(keyvault.key_id_of(secret))
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

    p_vault = sub.add_parser("keyvault", help="sealed copies of API keys: status / re-seal")
    p_vault.add_argument("action", choices=["status", "reseal", "key-id"])
    p_vault.add_argument("--yes", action="store_true",
                         help="reseal without asking (for scripts)")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.cmd == "keyvault":
        if args.action == "key-id":
            return _key_id()
        return asyncio.run(_keyvault(args.action, args.yes))

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
