#!/usr/bin/env python3
"""Which live keys would change permission if membership started counting.

PRD v1.4 option A makes an API key inherit its model list from the workspaces
its owner belongs to. Today membership is bookkeeping only: it records who is
in which class and grants nothing. Turning it on is therefore not a feature
addition - it silently re-permissions every key already in circulation.

This reads a running gateway's database and says exactly which keys would gain
access, which would lose it, and which are untouched. It writes nothing.

    python scripts/access_change_report.py                       # uses GW_DATABASE_URL
    python scripts/access_change_report.py --db sqlite:///data/gateway.db
    python scripts/access_change_report.py --json                # for a diff over time

Read the "loses access" section first. Anything there is a person who can call
a model this morning and cannot this afternoon, without having done anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
except ImportError:  # pragma: no cover - the venv always has it
    sys.exit("sqlalchemy is not installed; run this from the gateway's venv")

# Read through the ORM, never with SQL of our own. A deployment that predates
# the course -> workspace rename still has `courses`, `enrollments` and
# `api_keys.course_id` on disk; the mapping carries the new names in Python and
# the old ones in the database. Hand-written SQL picks whichever of the two the
# author happened to remember and breaks on half the fleet.
from app.db.models import ApiKey, Membership, ModelRecord, User, Workspace, WorkspaceModel


def sync_url(url: str) -> str:
    """The app talks async; a report has no reason to."""
    return url.replace("+aiosqlite", "").replace("+asyncpg", "")


def load(engine):
    with Session(engine) as db:
        keys = [
            {
                "id": k.id, "name": k.name, "key_prefix": k.key_prefix,
                "workspace_id": k.workspace_id, "models": k.models or [],
                "user_id": u.id, "external_id": u.external_id,
                "display_name": u.display_name, "role": u.role,
            }
            for k, u in db.execute(
                select(ApiKey, User).join(User, User.id == ApiKey.user_id)
                .where(ApiKey.revoked_at.is_(None))
            )
        ]

        members = defaultdict(set)
        for user_id, workspace_id in db.execute(
            select(Membership.user_id, Membership.workspace_id)
        ):
            members[user_id].add(workspace_id)

        allowed = defaultdict(set)
        for workspace_id, alias in db.execute(
            select(WorkspaceModel.workspace_id, WorkspaceModel.model_alias)
        ):
            allowed[workspace_id].add(alias)

        names = {i: code for i, code in db.execute(select(Workspace.id, Workspace.code))}
        catalogue = {
            alias for (alias,) in db.execute(
                select(ModelRecord.alias).where(ModelRecord.enabled.is_(True))
            )
        }
    return keys, members, allowed, names, catalogue


def permitted_today(key, allowed, catalogue) -> set[str]:
    """What this key can reach right now.

    Only two things narrow a key today: the workspace it was pinned to at issue
    time, and the per-key list. Membership is not consulted anywhere.
    """
    reach = set(catalogue)
    if key["workspace_id"]:
        reach &= allowed.get(key["workspace_id"], set())
    on_key = key["models"]
    if on_key:
        reach &= set(on_key)
    return reach


def permitted_under_a(key, members, allowed, catalogue) -> set[str]:
    """What it would reach if membership counted, as a union across groups.

    A key pinned to a workspace keeps winning (FR-42). A key with no pin and an
    owner in no group keeps the whole catalogue - the alternative, an empty set,
    would lock out every key issued before workspaces were used at all.
    """
    if key["workspace_id"]:
        reach = set(allowed.get(key["workspace_id"], set()))
    else:
        groups = members.get(key["user_id"], set())
        reach = set(catalogue) if not groups else set().union(
            *(allowed.get(w, set()) for w in groups)
        )
    on_key = key["models"]
    if on_key:
        reach &= set(on_key)
    return reach


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("GW_DATABASE_URL", ""))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if not args.db:
        return int(bool(sys.stderr.write(
            "no database: pass --db or set GW_DATABASE_URL\n"
        ))) or 2

    engine = create_engine(sync_url(args.db))
    keys, members, allowed, names, catalogue = load(engine)

    losers, gainers, same = [], [], []
    for key in keys:
        before = permitted_today(key, allowed, catalogue)
        after = permitted_under_a(key, members, allowed, catalogue)
        entry = {
            "key": key["key_prefix"],
            "label": key["name"] or "",
            "owner": key["external_id"],
            "owner_name": key["display_name"] or "",
            "role": key["role"],
            "workspaces": sorted(names.get(w, w) for w in members.get(key["user_id"], set())),
            "pinned_to": names.get(key["workspace_id"], "") if key["workspace_id"] else "",
            "before": sorted(before),
            "after": sorted(after),
            "loses": sorted(before - after),
            "gains": sorted(after - before),
        }
        (losers if entry["loses"] else gainers if entry["gains"] else same).append(entry)

    if args.json:
        print(json.dumps({"loses": losers, "gains": gainers, "unchanged": same}, indent=2))
        return 1 if losers else 0

    print(f"\nโมเดลใน registry: {len(catalogue)} · key ที่ยังไม่ถูกเพิกถอน: {len(keys)}\n")

    if losers:
        print(f"── เสียสิทธิ์ {len(losers)} ใบ ─────────────────────────────────────")
        print("  ใบพวกนี้เรียกโมเดลได้อยู่วันนี้ และจะเรียกไม่ได้ทันทีที่เปลี่ยน\n")
        for e in losers:
            groups = ", ".join(e["workspaces"]) or "ไม่ได้อยู่กลุ่มไหน"
            print(f"  {e['key']}… · {e['owner']} ({e['owner_name']}) · {e['label']}")
            print(f"      กลุ่ม: {groups}")
            print(f"      เสีย: {', '.join(e['loses'])}")
            print(f"      เหลือ: {', '.join(e['after']) or '— ไม่เหลืออะไรเลย —'}\n")
    else:
        print("── ไม่มีใบไหนเสียสิทธิ์ ──────────────────────────────────────\n")

    if gainers:
        print(f"── ได้สิทธิ์เพิ่ม {len(gainers)} ใบ ───────────────────────────────")
        for e in gainers:
            print(f"  {e['key']}… · {e['owner']} · เพิ่ม: {', '.join(e['gains'])}")
        print()

    print(f"── ไม่เปลี่ยน {len(same)} ใบ ─────────────────────────────────────\n")

    if losers:
        print("ก่อนเปลี่ยน: ธง grandfather (FR-43) ต้องคุ้ม key พวกนี้ หรือแจ้งเจ้าของ")
        print("ก่อนถึงวันเปลี่ยน ไม่ใช่ให้ไปเจอเองตอนเรียกไม่ได้\n")
    return 1 if losers else 0


if __name__ == "__main__":
    raise SystemExit(main())
