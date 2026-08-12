#!/usr/bin/env python3
"""Enrol a list of people from a CSV.

A pilot means thirty or so members, each needing a user, a place in a
workspace, and a key to actually call the gateway with. Done through the API by
hand that is ninety requests, and the failure mode is not that it is tedious —
it is that somebody stops halfway, retries, and ends up with duplicate users or
members holding two keys nobody can account for.

So this is **re-runnable**. A user that already exists is left exactly as it is;
a member who already holds a live key does not get a second one. Run it again
after adding five names to the file and five people are enrolled. That property
is what makes it usable during a pilot, when the list changes every week.

It goes through the admin API rather than the database, so every rule the
gateway enforces — role validation, key format, the audit log — applies here
too. A provisioning tool that writes rows directly is a second implementation
of the rules, and it will drift.

Usage:

    export LITEGATE_URL=https://gateway.uni.ac.th
    export LITEGATE_ADMIN_KEY=lg_sk_...
    python scripts/provision.py members.csv --workspace ai-101 --out keys.csv

The CSV needs an `external_id` column. `display_name`, `email` and `role` are
optional:

    external_id,display_name,email,role
    s6412345,Somchai P.,s6412345@uni.ac.th,member
    t0001,Dr Anong,anong@uni.ac.th,manager

Keys are shown once, by the gateway, and never again. They are written to
`--out` **as each one is issued**, not collected up and saved at the end: the
first version did that, the write failed on the last line, and three keys that
existed nowhere else were gone. The file is full of credentials, so it is
created mode 600 and should be handed out and then deleted.

If a key does go missing, the member is not stuck — revoke it in the console
and run this again; a member with no live key gets a new one.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(30.0)
VALID_ROLES = {"member", "manager", "admin"}


class Provisioner:
    def __init__(self, base_url: str, admin_key: str, dry_run: bool = False) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=TIMEOUT, headers={"Authorization": f"Bearer {admin_key}"}
        )
        self._dry_run = dry_run

    # -- helpers ----------------------------------------------------------
    def _get(self, path: str, **params) -> dict[str, Any]:
        response = self._client.get(f"{self._base}{path}", params=params)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict) -> dict[str, Any]:
        response = self._client.post(f"{self._base}{path}", json=body)
        if response.status_code >= 400:
            raise RuntimeError(_message(response))
        return response.json()

    # -- the pieces -------------------------------------------------------
    def existing_users(self) -> dict[str, dict]:
        """Everyone the gateway already knows, by external_id.

        Fetched once rather than probed per row: a pilot list is small, and one
        request beats thirty that each say "does this person exist yet".
        """
        users = self._get("/admin/users", limit=1000).get("data", [])
        return {u["external_id"]: u for u in users}

    def ensure_user(self, row: dict, existing: dict[str, dict]) -> tuple[dict, str]:
        external_id = row["external_id"]
        if external_id in existing:
            return existing[external_id], "already enrolled"
        if self._dry_run:
            return {"id": "(dry-run)", "external_id": external_id}, "would create"
        user = self._post(
            "/admin/users",
            {
                "external_id": external_id,
                "display_name": row.get("display_name") or "",
                "email": row.get("email") or None,
                "role": row.get("role") or "member",
            },
        )
        return user, "created"

    def ensure_membership(self, user_id: str, workspace_id: str) -> None:
        if self._dry_run or not workspace_id:
            return
        # Joining twice is not an error here: the gateway answers 200 with
        # status "already_joined", which is exactly what a re-run should get.
        self._post(f"/admin/workspaces/{workspace_id}/join", {"user_id": user_id})

    def has_live_key(self, user_id: str) -> bool:
        """Whether this person holds a key they can actually use.

        The listing reports `revoked`, not `revoked_at`. Reading the wrong field
        made every revoked key look live, which silently broke the one recovery
        path there is: revoke a lost key, run again, get a new one.
        """
        keys = self._get("/admin/api-keys", user_id=user_id).get("data", [])
        return any(not key.get("revoked") for key in keys)

    def issue_key(self, user: dict, workspace_id: str, expires: int | None) -> str | None:
        """Issue a key, unless this person already holds one.

        Re-running must not scatter extra keys: keys are what nobody cleans up,
        and a member with four of them is one nobody can audit.
        """
        if self._dry_run:
            return "(dry-run)"
        if self.has_live_key(user["id"]):
            return None
        created = self._post(
            "/admin/api-keys",
            {
                "user_id": user["id"],
                "workspace_id": workspace_id or None,
                "name": "provisioned",
                "expires_in_days": expires,
            },
        )
        # The gateway calls it `api_key`, and returns it exactly once.
        return created["api_key"]


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    error = body.get("error") or body
    return f"HTTP {response.status_code}: {error.get('message') or body}"


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [
            {k.strip(): (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(handle)
        ]

    problems = []
    seen: set[str] = set()
    cleaned = []
    for number, row in enumerate(rows, start=2):  # line 1 is the header
        external_id = row.get("external_id", "")
        if not external_id:
            problems.append(f"line {number}: no external_id")
            continue
        if external_id in seen:
            # Two rows for one person is a typo in the source list, and going
            # ahead would issue whichever role came last, quietly.
            problems.append(f"line {number}: '{external_id}' appears twice")
            continue
        role = row.get("role") or "member"
        if role not in VALID_ROLES:
            problems.append(f"line {number}: unknown role '{role}'")
            continue
        seen.add(external_id)
        cleaned.append(row)

    if problems:
        # Refuse the file rather than enrol the good half: a partial run leaves
        # nobody able to say who is in and who is not.
        raise SystemExit(
            "The list has problems; nothing was changed:\n  " + "\n  ".join(problems)
        )
    return cleaned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--workspace", default="", help="workspace id to enrol into")
    parser.add_argument("--out", type=Path, default=Path("provisioned-keys.csv"))
    parser.add_argument("--expires-days", type=int, default=180)
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would happen, change nothing"
    )
    args = parser.parse_args()

    base_url = os.environ.get("LITEGATE_URL", "").strip()
    admin_key = os.environ.get("LITEGATE_ADMIN_KEY", "").strip()
    if not base_url or not admin_key:
        raise SystemExit("Set LITEGATE_URL and LITEGATE_ADMIN_KEY.")

    rows = read_rows(args.csv_file)
    print(f"{len(rows)} people in {args.csv_file}")
    if args.dry_run:
        print("(dry run — nothing will be changed)")

    provisioner = Provisioner(base_url, admin_key, dry_run=args.dry_run)
    existing = provisioner.existing_users()

    counts = {"created": 0, "already enrolled": 0, "keys issued": 0, "kept key": 0}

    # Opened before a single key is issued. A key exists in exactly one place -
    # the reply that created it - so discovering the output path is unwritable
    # *after* issuing thirty of them destroys thirty credentials. Ask first.
    keyfile = None
    writer = None
    if not args.dry_run:
        try:
            keyfile = args.out.open("w", newline="", encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"Cannot write {args.out}: {exc}. Nothing was changed.") from exc
        args.out.chmod(0o600)
        writer = csv.writer(keyfile)
        writer.writerow(["external_id", "api_key"])
        keyfile.flush()

    issued = 0
    for row in rows:
        try:
            user, what = provisioner.ensure_user(row, existing)
            counts[what if what in counts else "created"] += 1
            provisioner.ensure_membership(user["id"], args.workspace)
            key = provisioner.issue_key(user, args.workspace, args.expires_days)
        except (RuntimeError, httpx.HTTPError) as exc:
            # Stop rather than skip. Half an enrolment is worse than none: you
            # cannot tell afterwards who was done and who was not.
            print(f"  {row['external_id']}: {exc}", file=sys.stderr)
            raise SystemExit(
                f"Stopped at '{row['external_id']}'. Fix the cause and run again — "
                "people already enrolled will be left alone."
            ) from exc

        if key:
            if writer is not None:
                # Flushed per key, not per run: a crash halfway should cost the
                # one key in flight, not every key issued before it.
                writer.writerow([row["external_id"], key])
                keyfile.flush()
            issued += 1
            counts["keys issued"] += 1
        else:
            counts["kept key"] += 1
        print(f"  {row['external_id']:<24} {what}{'  + key' if key else '  (has a key)'}")

    print()
    print("  ".join(f"{name}: {count}" for name, count in counts.items()))

    if keyfile is not None:
        keyfile.close()
        if issued:
            print(f"\n{issued} key(s) in {args.out} (mode 600)")
            print("The gateway shows a key once. Hand these out, then delete the file.")
        else:
            args.out.unlink(missing_ok=True)
            print("\nNo new keys — everybody already had one.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
