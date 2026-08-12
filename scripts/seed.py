#!/usr/bin/env python3
"""Seed a workspace, members and API keys for a first run or a demo.

    python scripts/seed.py --workspace CS101 --name "Intro to Programming" \
        --members 6412345678,6412345679 --models coding,gemma-vision

Keys are printed once. They are stored only as HMAC digests.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.auth import generate_api_key  # noqa: E402
from app.db.models import ApiKey, Membership, User, Workspace, WorkspaceModel  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.registry.store import load_snapshot  # noqa: E402


async def seed(
    workspace_code: str,
    workspace_name: str,
    term: str,
    member_ids: list[str],
    model_aliases: list[str],
    config_dir: Path,
) -> None:
    await init_db()

    snapshot = load_snapshot(config_dir)
    unknown = [a for a in model_aliases if a not in snapshot.models]
    if unknown:
        raise SystemExit(
            f"unknown model alias(es): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(snapshot.models))}"
        )

    issued: list[tuple[str, str]] = []

    async with session_scope() as session:
        result = await session.execute(select(Workspace).where(Workspace.code == workspace_code))
        workspace = result.scalar_one_or_none()
        if workspace is None:
            workspace = Workspace(code=workspace_code, name=workspace_name, term=term)
            session.add(workspace)
            await session.flush()
            print(f"created workspace {workspace_code}")
        else:
            print(f"workspace {workspace_code} already exists")

        for alias in model_aliases:
            exists = await session.execute(
                select(WorkspaceModel).where(
                    WorkspaceModel.workspace_id == workspace.id, WorkspaceModel.model_alias == alias
                )
            )
            if exists.scalar_one_or_none() is None:
                session.add(
                    WorkspaceModel(workspace_id=workspace.id, model_alias=alias, enabled=True)
                )
        print(f"allowed models: {', '.join(model_aliases)}")

        for external_id in member_ids:
            found = await session.execute(
                select(User).where(User.external_id == external_id)
            )
            user = found.scalar_one_or_none()
            if user is None:
                user = User(
                    external_id=external_id,
                    display_name=f"Member {external_id}",
                    role="member",
                )
                session.add(user)
                await session.flush()

            joined = await session.execute(
                select(Membership).where(
                    Membership.user_id == user.id, Membership.workspace_id == workspace.id
                )
            )
            if joined.scalar_one_or_none() is None:
                session.add(Membership(user_id=user.id, workspace_id=workspace.id))

            plaintext, prefix, digest = generate_api_key()
            session.add(
                ApiKey(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    name=f"{workspace_code} key",
                    key_prefix=prefix,
                    key_hash=digest,
                    scopes=[],
                )
            )
            issued.append((external_id, plaintext))

    print("\n" + "=" * 78)
    print("API KEYS (shown once - distribute securely, they cannot be recovered)")
    print("=" * 78)
    for external_id, key in issued:
        print(f"{external_id:<16} {key}")
    print("=" * 78 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed workspaces and member keys")
    parser.add_argument("--workspace", required=True, help="workspace code, e.g. CS101")
    parser.add_argument("--name", default="", help="workspace display name")
    parser.add_argument("--term", default="1/2569")
    parser.add_argument(
        "--members", required=True, help="comma-separated member IDs"
    )
    parser.add_argument(
        "--models", default="coding", help="comma-separated model aliases to allow"
    )
    parser.add_argument("--config-dir", default="./config")
    args = parser.parse_args()

    members = [s.strip() for s in args.members.split(",") if s.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    asyncio.run(
        seed(
            args.workspace,
            args.name or args.workspace,
            args.term,
            members,
            models,
            Path(args.config_dir),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
