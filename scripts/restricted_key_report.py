#!/usr/bin/env python3
"""Which live keys stop being administrator keys when the gateway is upgraded.

Until 2026-10 an API key carried its owner's role whatever was written on it: a
key an administrator issued for one script and limited to one model could still
call every /admin route, including the one that lifts its own limit. From this
release a key that carries any limit of its own - a model list, an access
group, a workspace, a quota of its own - has no admin or manager rights. It
calls its models exactly as before.

That is a behaviour change for keys already in circulation: a scheduled job
that uses such a key for admin calls gets 403 the moment the new version
starts. This reads a gateway's database and says which keys those are. It
writes nothing - a SQLite file is opened read-only.

    python scripts/restricted_key_report.py --db /opt/litegate/data/gateway.db
    python scripts/restricted_key_report.py                    # uses GW_DATABASE_URL
    python scripts/restricted_key_report.py --db <url|path> --json

It prints a key's id, prefix, label and its owner's role. Never the key, never
its hash - neither is read from the database at all.

The second section is the other change in the same release: a key limited only
to access groups that are switched off or gone used to call everything, and now
calls nothing.

Exit status: 0 nothing changes, 1 at least one key changes, 2 could not run.
Status 1 is only ever the answer to the question: every way of failing to read
the database - no such file, not a gateway database, no permission, no driver,
no server - is 2, with the reason on stderr and nothing on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

try:
    from sqlalchemy import create_engine, inspect, literal, select
    from sqlalchemy.orm import Session
except ImportError:  # pragma: no cover - the venv always has it
    sys.exit("sqlalchemy is not installed; run this from the gateway's venv")

# กติกามาจากที่เดียวกับด่านจริง: รายงานที่นิยาม "ถูกจำกัด" เองจะเริ่มตอบไม่ตรงกับเกตเวย์
# ในวันที่มีคนเพิ่มข้อจำกัดชนิดใหม่ แล้วไม่มีอะไรแดง
from app.core.auth import (  # noqa: E402
    PRIVILEGED_ROLES,
    cap_still_runs,
    limits_on_key,
    normalise_role,
)
from app.db.models import AccessGroup, ApiKey, QuotaPolicy, User  # noqa: E402


class CannotRun(Exception):
    """อ่านฐานข้อมูลไม่ได้ — `main` แปลงเป็น exit status 2

    ไม่ใช้ `sys.exit("ข้อความ")`: Python จบแบบนั้นด้วย status 1 ซึ่งในสคริปต์นี้แปลว่า "มี
    key ที่เปลี่ยน" · สคริปต์ deploy ที่แตกกิ่งตาม status จึงอ่าน "ไม่พบไฟล์ฐานข้อมูล" เป็น
    "มีใบกระทบ" (ทีมเอกสารรันเจอ 2026-10-09)
    """


LIMIT_WORDS = {
    "models": "รายการโมเดลบนใบ",
    "access_groups": "มัดโมเดลบนใบ",
    "workspace": "ผูก workspace",
    "cap": "เพดานโควตาเฉพาะใบ",
}


def _sync_url(url: str) -> str:
    """URL แบบ sync ของฐานเดียวกัน · SQLite ทำเองที่นี่ · PostgreSQL ยืมตัวเลือกไดรเวอร์จากรายงานพี่น้อง

    เดิม import `access_change_report` ตั้งแต่บรรทัดบนสุดของไฟล์ — เคสจริง 2026-10-09 บนเครื่องเกตเวย์
    หลังอัปเดตเป็น 1.13.0 ผ่านปุ่ม Update (ปุ่มไม่ติดตั้ง scripts/): ก๊อปไฟล์นี้ไปไฟล์เดียวตามเอกสาร แล้วรัน
    กับฐาน SQLite ตัวจริง ได้ traceback `No module named 'access_change_report'` และ status 1
    ซึ่งในสคริปต์นี้แปลว่า "มี key ที่เปลี่ยน" · ไฟล์พี่น้องจำเป็นเฉพาะตอนเลือกไดรเวอร์ sync ของ
    PostgreSQL — ฐาน SQLite ไม่ควรล้มเพราะไม่มีมัน
    """
    if "sqlite" in url:
        return url.replace("+aiosqlite", "")       # ตรงกับ access_change_report.sync_url
    try:
        from access_change_report import sync_url
    except ImportError:
        raise CannotRun(
            "ฐานข้อมูลนี้ไม่ใช่ SQLite — ต้องมี scripts/access_change_report.py อยู่ข้างไฟล์นี้ "
            "(ใช้เลือกไดรเวอร์ของ PostgreSQL)\n"
            "ก๊อปมาจาก checkout ของรุ่นเดียวกัน หรือรันรายงานนี้จาก checkout นั้นตรง ๆ"
        ) from None
    return sync_url(url)


def read_only_url(target: str) -> str:
    """URL ที่เปิดฐานข้อมูลได้โดยเขียนไม่ได้ · รับได้ทั้ง URL และ path ของไฟล์ SQLite

    `mode=ro` ไม่ใช่ `immutable=1`: เกตเวย์ที่รันอยู่ใช้ WAL และของที่เพิ่ง commit ยังอยู่ใน
    ไฟล์ -wal — immutable ข้ามไฟล์นั้นไปเลย รายงานจะอ่านฐานของเมื่อวานแล้วบอกว่าไม่มีอะไร
    เปลี่ยน · mode=ro อ่าน WAL ครบ และไดรเวอร์เป็นคนปฏิเสธการเขียนเอง ไม่ใช่เรา "สัญญา"
    ว่าจะไม่เขียน
    """
    url = target
    if "://" not in url:
        url = f"sqlite:///{Path(target).expanduser().resolve()}"
    try:
        url = _sync_url(url)
    except SystemExit as stop:
        # รายงานพี่น้องจบด้วย `sys.exit(ข้อความ)` เมื่อไม่มีไดรเวอร์ sync ของ Postgres —
        # ข้อความของมันบอกวิธีแก้ครบแล้ว เปลี่ยนแค่ status
        raise CannotRun(str(stop.code)) from None
    if not url.startswith("sqlite"):
        return url
    path = url.split("///", 1)[1].split("?", 1)[0]
    if not path or path == ":memory:":
        raise CannotRun("ต้องเป็นไฟล์ฐานข้อมูลของเกตเวย์ ไม่ใช่ฐานในหน่วยความจำ")
    # .env ของเกตเวย์เขียน path แบบสัมพัทธ์ (`sqlite+aiosqlite:///./data/gateway.db`)
    file = Path(path).expanduser().resolve()
    # ไม่มีไฟล์แล้ว sqlite จะบอกว่า "unable to open database file" ซึ่งอ่านเหมือนสิทธิ์ไม่พอ
    if not file.is_file():
        raise CannotRun(f"ไม่พบไฟล์ฐานข้อมูล: {file}")
    return f"sqlite:///file:{file}?mode=ro&uri=true"


def _present(engine, model, *names):
    """คอลัมน์ที่ฐานนี้มีจริง · ตัวที่ยังไม่มีได้ NULL แทน และถูกจดไว้ใน `missing`

    ฐานของเครื่องที่ยังไม่ได้อัปเกรดคือฐานที่รายงานนี้ต้องอ่านให้ได้ — ตารางของรุ่นเก่าอาจยัง
    ไม่มี `access_groups` หรือ `quota_policies.api_key_id` การ SELECT ทั้ง ORM จะล้มด้วย
    "no such column" ก่อนได้บอกอะไรเลย · คอลัมน์ที่ไม่มี = ข้อจำกัดชนิดนั้นยังไม่มีในฐานนี้
    """
    table = model.__table__
    inspector = inspect(engine)
    if not inspector.has_table(table.name):
        return None, [f"{table.name} (ทั้งตาราง)"]
    have = {c["name"] for c in inspector.get_columns(table.name)}
    columns, missing = [], []
    for name in names:
        column = getattr(model, name)
        if column.property.columns[0].name in have:
            columns.append(column)
        else:
            columns.append(literal(None).label(name))
            missing.append(f"{table.name}.{column.property.columns[0].name}")
    return columns, missing


def load(engine) -> dict:
    """ทุกอย่างที่กติกาต้องใช้ · ไม่มี key_hash และ key_sealed ใน SELECT ไหนเลย"""
    missing: list[str] = []
    now = datetime.now(timezone.utc)

    with Session(engine) as db:
        columns, gone = _present(
            engine, ApiKey, "id", "user_id", "workspace_id", "name", "key_prefix",
            "scopes", "models", "access_groups", "expires_at", "last_used_at",
        )
        if columns is None:
            # ตารางของข้อจำกัด (มัด · เพดาน) ขาดได้ — ฐานของรุ่นเก่า · ตารางของ key เองขาด
            # ไม่ได้: ไฟล์นี้ไม่ใช่ฐานของเกตเวย์ และ "0 ใบ ไม่มีอะไรเปลี่ยน" คือคำตอบที่ผิด
            raise CannotRun(
                f"ไม่มีตาราง {ApiKey.__tablename__} — ไฟล์นี้ไม่ใช่ฐานข้อมูลของเกตเวย์"
            )
        missing += gone
        keys = [
            dict(zip(
                ("id", "user_id", "workspace_id", "name", "key_prefix", "scopes",
                 "models", "access_groups", "expires_at", "last_used_at", "role"),
                row, strict=True,
            ))
            for row in db.execute(
                select(*columns, User.role)
                .join(User, User.id == ApiKey.user_id)
                .where(ApiKey.revoked_at.is_(None))
                .order_by(ApiKey.created_at, ApiKey.id)
            )
        ]

        capped: set[str] = set()
        columns, gone = _present(engine, QuotaPolicy, "api_key_id", "enabled", "expires_at")
        missing += gone
        if columns is not None and not gone:
            for api_key_id, enabled, expires_at in db.execute(
                select(*columns).where(QuotaPolicy.api_key_id.is_not(None))
            ):
                if enabled and cap_still_runs(expires_at, now):
                    capped.add(api_key_id)

        # มัดที่ยังให้อะไรอยู่จริง: เปิดอยู่ และมีโมเดลอย่างน้อยหนึ่งตัว
        granting: set[str] = set()
        columns, gone = _present(engine, AccessGroup, "id", "enabled", "models")
        missing += gone
        if columns is not None and not gone:
            for group_id, enabled, models in db.execute(select(*columns)):
                if enabled and models:
                    granting.add(group_id)

    return {"keys": keys, "capped": capped, "granting": granting,
            "missing": missing, "now": now}


def describe(key: dict, data: dict) -> dict:
    """หนึ่งแถวของรายงาน · ชื่อฟิลด์เดียวกับที่ `GET /v1/me/key` ใช้"""
    role = normalise_role(key["role"] or "")
    limits = limits_on_key(
        models=key["models"], access_groups=key["access_groups"],
        workspace_id=key["workspace_id"], capped=key["id"] in data["capped"],
    )
    expires = key["expires_at"]
    named = list(key["access_groups"] or [])
    return {
        "id": key["id"],
        "key_prefix": key["key_prefix"],
        "name": key["name"] or "",
        "owner_role": role,
        "limited_by": list(limits),
        "loses_admin_access": role in PRIVILEGED_ROLES and bool(limits),
        # ระบุแต่มัด และไม่มีมัดไหนให้อะไรเลย: เดิมเรียกได้ทุกโมเดล ตอนนี้เรียกไม่ได้สักตัว
        "bundles_grant_nothing": bool(named) and not key["models"]
        and not any(group in data["granting"] for group in named),
        "expired": expires is not None and not cap_still_runs(expires, data["now"]),
        "last_used_at": key["last_used_at"].isoformat() if key["last_used_at"] else None,
        "scopes": list(key["scopes"] or []),
    }


def build(engine) -> dict:
    data = load(engine)
    rows = [describe(key, data) for key in data["keys"]]
    return {
        "live_keys": len(rows),
        "loses_admin_access": [r for r in rows if r["loses_admin_access"]],
        "bundles_grant_nothing": [r for r in rows if r["bundles_grant_nothing"]],
        "unchanged": [r for r in rows
                      if not r["loses_admin_access"] and not r["bundles_grant_nothing"]],
        "columns_missing": data["missing"],
    }


def _line(row: dict) -> str:
    used = row["last_used_at"] or "ไม่เคยใช้"
    return (
        f"  {row['key_prefix']}… · id {row['id']} · {row['name'] or '(ไม่มีชื่อ)'}"
        f" · เจ้าของเป็น {row['owner_role']}"
        f"{' · หมดอายุแล้ว' if row['expired'] else ''}\n"
        f"      จำกัดด้วย: {', '.join(LIMIT_WORDS[n] for n in row['limited_by'])}"
        f" · ใช้ล่าสุด: {used}\n"
    )


def render(report: dict) -> str:
    out: list[str] = []
    losers, dead, same = (report["loses_admin_access"], report["bundles_grant_nothing"],
                          report["unchanged"])
    out.append(f"\nkey ที่ยังไม่ถูกเพิกถอน: {report['live_keys']} ใบ\n")

    if report["columns_missing"]:
        out.append("  ฐานนี้ยังไม่มี: " + ", ".join(report["columns_missing"]))
        out.append("  (ของรุ่นเก่ากว่า — ข้อจำกัดชนิดนั้นยังไม่มีใครตั้งได้ จึงนับว่าไม่มี)\n")

    if losers:
        out.append(f"── เสียสิทธิ์ผู้ดูแล {len(losers)} ใบ ─────────────────────────────────")
        out.append("  ใบพวกนี้เรียก /admin/* ได้อยู่วันนี้ และจะได้ 403 ทันทีที่อัปเกรด")
        out.append("  ยังเรียกโมเดลได้ตามเดิมทุกอย่าง\n")
        out += [_line(row) for row in losers]
        out.append("  ถ้าใบไหนมีงานดูแลระบบใช้อยู่ ก่อนอัปเกรดให้ทำอย่างใดอย่างหนึ่ง:")
        out.append("    · ออกใบใหม่ให้งานนั้นโดยไม่ใส่ข้อจำกัด แล้วเปลี่ยนในสคริปต์")
        out.append("    · หรือถอดข้อจำกัดของใบเดิมในคอนโซล — รายการโมเดล: Access → Models…")
        out.append("      เพดานเฉพาะใบ: แท็บ Quota · มัดกับ workspace ที่ผูกไว้ถอดจากใบเดิมไม่ได้")
        out.append("      (PATCH /admin/api-keys รับแค่ days กับ models) ต้องออกใบใหม่")
        out.append("  ใบที่ใช้เรียกโมเดลอย่างเดียว ไม่ต้องทำอะไร — นั่นคือสิ่งที่กติกานี้ตั้งใจ\n")
    else:
        out.append("── ไม่มีใบไหนเสียสิทธิ์ผู้ดูแล ─────────────────────────────────\n")

    if dead:
        out.append(f"── จำกัดไว้ด้วยมัดที่ไม่ให้อะไรเลย {len(dead)} ใบ ────────────────────")
        out.append("  มัดที่ใบพวกนี้ระบุถูกปิด ถูกลบ หรือว่างเปล่า · วันนี้ใบเรียกได้ทุกโมเดล")
        out.append("  (บั๊ก) หลังอัปเกรดจะเรียกไม่ได้สักตัว จนกว่าจะเปิดมัดกลับหรือใส่รายการให้ใบ\n")
        out += [_line(row) for row in dead]
    else:
        out.append("── ไม่มีใบไหนจำกัดไว้ด้วยมัดที่ไม่ให้อะไร ─────────────────────────\n")

    out.append(f"── ไม่เปลี่ยน {len(same)} ใบ ─────────────────────────────────────")
    keeps = [r for r in same if r["owner_role"] in PRIVILEGED_ROLES]
    if keeps:
        out.append("  ใบของ admin/manager ที่ยังมีสิทธิ์ผู้ดูแลเหมือนเดิม (ไม่มีข้อจำกัดบนใบ):")
        out += [
            f"    {r['key_prefix']}… · {r['name'] or '(ไม่มีชื่อ)'} · {r['owner_role']}"
            + (f" · scopes {r['scopes']} (ไม่ถูกนับ — ไม่เคยถูกบังคับ)" if r["scopes"] else "")
            for r in keeps
        ]
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=os.environ.get("GW_DATABASE_URL", ""),
                        help="database URL, or the path of a SQLite file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.db:
        sys.stderr.write("no database: pass --db or set GW_DATABASE_URL\n")
        return 2

    try:
        report = read(args.db)
    except CannotRun as stop:
        sys.stderr.write(f"รันไม่ได้ — {stop}\n")
        return 2

    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 1 if report["loses_admin_access"] or report["bundles_grant_nothing"] else 0


def read(target: str) -> dict:
    """รายงานทั้งใบ หรือ `CannotRun` — ไม่มีครึ่งทาง

    ทุกอย่างที่ล้มระหว่างเปิดและอ่านฐานถูกจับที่นี่ที่เดียว: ไฟล์ที่ไม่ใช่ฐานข้อมูล · ไม่มีสิทธิ์
    อ่าน · ไฟล์ SQLite ที่ไม่มีตารางของเกตเวย์ · เซิร์ฟเวอร์ที่ต่อไม่ติด · ถ้าปล่อยให้หลุดออก
    ไปเป็น traceback Python จะจบด้วย status 1 เหมือนกัน

    ข้อความมาจากไดรเวอร์ (`exc.orig`) ไม่ใช่จาก URL ที่รับเข้ามา — URL ของ PostgreSQL มี
    รหัสผ่านอยู่ในตัว และ stderr ของสคริปต์นี้ถูกแปะลง ticket ได้เท่ากับ stdout
    """
    try:
        engine = create_engine(read_only_url(target))
    except CannotRun:
        raise
    except Exception as exc:  # noqa: BLE001 - URL ที่ SQLAlchemy อ่านไม่ออก ก็คือรันไม่ได้
        raise CannotRun(f"URL ของฐานข้อมูลใช้ไม่ได้ ({type(exc).__name__})") from None
    try:
        return build(engine)
    except CannotRun:
        raise
    except Exception as exc:  # noqa: BLE001 - ดู docstring: ทุกทางที่ล้มคือ status 2
        cause = getattr(exc, "orig", None) or exc
        said = (str(cause).strip().splitlines() or [type(cause).__name__])[0]
        raise CannotRun(
            f"อ่านฐานข้อมูลไม่ได้: {said}\n"
            "  ตรวจว่า --db ชี้ไปที่ฐานข้อมูลของเกตเวย์ และผู้ใช้นี้อ่านได้"
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
