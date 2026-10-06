"""The audit trail: one row per decision somebody made.

Lives here rather than in `app/api/admin.py` because the admin plane is not the
only place decisions are made: issuing yourself a key, changing a password and
signing out are all changes to who can do what, and none of them is an admin
route.

**Writing the row must never be what fails the request.** The row is added to
the same transaction as the change it records, so a row the database refuses
takes the change down with it - and the refusal arrives at commit time, as an
HTTP 500 with nothing on screen about why.

That is exactly what a too-long `target_id` does on PostgreSQL. The column is
VARCHAR(128); SQLite ignores the length, PostgreSQL enforces it. Deleting a
quota policy wrote `"<32-char id> scope=workspace window=month requests=1000
user=- workspace=<32-char id> model=coding"` into it - 138 characters for an
ordinary workspace-and-model policy - so on the production database the delete
rolled back and the policy stayed, while every test on SQLite passed.

So the function below owns the guarantee instead of trusting each caller:

  * `target_id` holds an id. Descriptive text belongs in `payload`, which is
    JSON and has no length.
  * If something too long arrives anyway, the full text moves into
    `payload["target_detail"]` and the column keeps the leading id (the part
    before the first space) or, failing that, a truncated prefix.
  * Every other string column is cut to its width for the same reason.

Nothing here knows the widths by heart: they are read from the model, so
widening a column is a one-line change in `app/db/models.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def _width(column: str) -> int:
    return int(AuditLog.__table__.c[column].type.length)


def _text(value: Any) -> str:
    # PostgreSQL refuses a NUL byte in any text value, whatever its length.
    return str(value if value is not None else "").replace("\x00", "")


def _fit(value: Any, column: str) -> str:
    return _text(value)[: _width(column)]


def fit_target(target_id: Any, payload: dict | None) -> tuple[str, dict]:
    """`(target_id, payload)` in a shape the table accepts.

    The caller's dict is copied, never edited in place: several callers hand
    over the request body they are about to return from.
    """
    detail = dict(payload or {})
    target = _text(target_id)
    limit = _width("target_id")
    if len(target) <= limit:
        return target, detail

    # Keep everything that was said - an audit entry that silently lost its
    # tail is worse than a long one - but keep it where there is room for it.
    detail.setdefault("target_detail", target)
    head, _, rest = target.partition(" ")
    if rest and 0 < len(head) <= limit:
        return head, detail
    return target[:limit], detail


async def audit(
    session: AsyncSession,
    request: Request,
    actor,
    action: str,
    target_type: str = "",
    target_id: str = "",
    payload: dict | None = None,
) -> None:
    """Add the row to the caller's transaction. The caller commits.

    `actor` is the Principal who did it. Never put a secret in `payload`: this
    table is readable by every administrator and kept longer than usage.
    """
    target, detail = fit_target(target_id, payload)
    session.add(
        AuditLog(
            actor_user_id=_fit(getattr(actor, "user_id", "") or "", "actor_user_id") or None,
            action=_fit(action, "action"),
            target_type=_fit(target_type, "target_type"),
            target_id=target,
            payload=detail,
            ip=_fit(request.client.host if request.client else "", "ip"),
        )
    )
