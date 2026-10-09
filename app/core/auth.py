"""API-key authentication and the request Principal.

Key format:  lg_sk_<43 url-safe base64 chars>   (256 bits of entropy)

Keys issued before the rename start with `edu_sk_` and keep working: a key is
verified by HMAC over the whole string, so the prefix is a label for a human
reading a key list, not part of the check.

Because the secret is high-entropy random (not a human password), a single
HMAC-SHA256 with a server-side pepper is the correct verification primitive:
it is constant-time comparable, unforgeable without the pepper, and fast enough
to run on every request. A slow KDF would only add latency to the hot path.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import ErrorCode, GatewayError
from app.db.models import (
    AccessGroup,
    ApiKey,
    Membership,
    QuotaPolicy,
    User,
    Workspace,
    WorkspaceAccessGroup,
    WorkspaceModel,
    utcnow,
)
from app.db.session import get_session

log = logging.getLogger(__name__)

# ถี่แค่ไหนถึงจะเขียน last_used_at ลง DB จริง — ดูเหตุผลในจุดที่ใช้
LAST_USED_STAMP_INTERVAL = timedelta(seconds=60)

KEY_PREFIX = "lg_sk_"

# Roles recorded before the rename. Rows are not rewritten on upgrade, so a
# manager stored as "instructor" must keep their privileges.
LEGACY_ROLES = {"student": "member", "instructor": "manager"}


def normalise_role(role: str) -> str:
    return LEGACY_ROLES.get(role, role)
PREFIX_LEN = 12


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, key_prefix, key_hash). Plaintext is never stored."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, plaintext[:PREFIX_LEN], hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    pepper = get_settings().api_key_pepper.encode()
    return hmac.new(pepper, plaintext.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# key ที่ถูกจำกัด ไม่พกอำนาจของเจ้าของ
# ---------------------------------------------------------------------------
# role เป็นของ *คน* · key เป็นของ *งานหนึ่งงาน* — ผู้ดูแลออกใบให้สคริปต์แล้วเขียนไว้ว่า
# "ใช้ได้แค่ coding" คือการบอกว่าใบนี้มีไว้ทำอะไร · เดิมอำนาจดูจาก role ของเจ้าของอย่าง
# เดียว ใบนั้นจึงเรียก `PATCH /admin/api-keys/<ตัวเอง>` ถอดรายการของตัวเอง ออกใบใหม่
# แก้ registry และเปิดดู key ของคนอื่นได้ทั้งหมด (ตรวจ 2026-10-09) — ข้อจำกัดบนใบ
# เป็นแค่คำขอร้อง และใบที่หลุดหนึ่งใบเท่ากับหลุดทั้งเกตเวย์
#
# อะไรนับเป็นข้อจำกัด — ทุกอย่างที่เขียนไว้ *บนใบ* เพื่อให้มันแคบลง และมีเส้นทาง
# /admin ที่ถอดออกได้ถ้าใบยังพกอำนาจอยู่:
#
#   models          รายการ alias บนใบ            ถอดด้วย PATCH /admin/api-keys/{id}
#   access_groups   มัดโมเดลบนใบ                แก้/ปิดมัดด้วย /admin/access-groups
#   workspace       ใบผูกกับ workspace เดียว     เพิ่มโมเดลให้ workspace นั้นเอง
#   cap             เพดานโควตาเฉพาะใบที่ยังมีผล    DELETE /admin/quota-policies/{id}
#
# อะไร *ไม่* นับ:
#
#   scopes          เก็บไว้แต่ไม่เคยถูกบังคับที่ไหน (`require_scope` ไม่มีใครเรียก) และใบ
#                   bootstrap ของทุกเครื่องมี ["admin"] — นับเมื่อไรคือถอดอำนาจของใบที่
#                   ผู้ดูแลทุกคนถืออยู่ในวันที่อัปเกรด ด้วยฟิลด์ที่คอนโซลไม่แสดงและแก้ไม่ได้
#   expires_at      จำกัดว่าใช้ได้ถึงเมื่อไร ไม่ได้จำกัดว่าทำอะไรได้
#   kind            ป้ายให้คนอ่าน ไม่เปลี่ยนกติกา (ดู ApiKey.kind)
#   โควตาของคน/workspace/ทั้งระบบ   ผูกกับเจ้าของ ไม่ได้เขียนบนใบ
#
# ชื่อชุดนี้ออกไปถึงผู้เรียกใน `details.limited_by` และ `scripts/restricted_key_report.py`
# ใช้ฟังก์ชันเดียวกันนี้ — รายงานก่อนอัปเกรดกับด่านจริงจึงตอบไม่ตรงกันไม่ได้
PRIVILEGED_ROLES = frozenset({"admin", "manager"})

KEY_LIMIT_WORDS = {
    "models": "a model list",
    "access_groups": "an access group",
    "workspace": "a workspace",
    "cap": "a quota of its own",
}


def limits_on_key(
    *, models=None, access_groups=None, workspace_id=None, capped: bool = False
) -> tuple[str, ...]:
    """ข้อจำกัดที่เขียนไว้บน key ใบหนึ่ง เรียงตามลำดับคงที่ · ว่าง = ไม่มีเลย"""
    found = []
    if models:
        found.append("models")
    if access_groups:
        found.append("access_groups")
    if workspace_id:
        found.append("workspace")
    if capped:
        found.append("cap")
    return tuple(found)


@dataclass
class Principal:
    """Everything the request path needs to know about the caller.

    `role` is the owner's - who the person is, and so what they can *reach*:
    which models they see, whether workspaces narrow them. `is_admin` and
    `is_manager` are about this credential - whether it carries the owner's
    power over other people and over the gateway. The two differ for exactly one
    kind of caller: a key somebody limited (see `limits_on_key` above).
    """

    user_id: str
    external_id: str
    role: str
    display_name: str
    api_key_id: str
    workspace_id: str | None
    scopes: list[str]
    # alias ที่ key ใบนี้ระบุไว้เอง · ว่าง = ไม่จำกัดเพิ่ม (ดู assert_model_permitted)
    key_models: list[str] = field(default_factory=list)
    # bundle ที่ระบุไว้บน key · อ่านคู่กับ key_models ทั้งคู่ตอบคำถามเดียวกัน
    key_access_groups: list[str] = field(default_factory=list)
    # "key" for a program, "session" for a signed-in human. Self-service actions
    # that mint credentials require a session: a leaked key must not be able to
    # mint more keys for itself.
    via: str = "key"
    # ใบนี้มีเพดานโควตาของตัวเองที่ยังมีผลอยู่ · `authenticate` ถามให้เฉพาะใบที่คำตอบ
    # เปลี่ยนอะไรได้ (เจ้าของเป็น admin/manager และยังไม่มีข้อจำกัดอื่น)
    key_capped: bool = False

    @property
    def key_limits(self) -> tuple[str, ...]:
        """What was written on this key to narrow it. Never anything for a session."""
        if self.via != "key":
            return ()
        return limits_on_key(
            models=self.key_models, access_groups=self.key_access_groups,
            workspace_id=self.workspace_id, capped=self.key_capped,
        )

    # อำนาจถูกตัดสินตรงนี้ที่เดียว ไม่ใช่ที่แต่ละเส้นทาง: `require_admin` ·
    # `require_manager` · ข้อยกเว้น scope ข้างล่าง และทุก `if actor.is_admin` ใน
    # app/api อ่านสองตัวนี้ — เส้นทางที่เขียนเพิ่มวันหน้าจึงลืมกติกานี้ไม่ได้
    @property
    def is_admin(self) -> bool:
        return self.role == "admin" and not self.key_limits

    @property
    def is_manager(self) -> bool:
        return self.role in PRIVILEGED_ROLES and not self.key_limits

    def require_scope(self, scope: str) -> None:
        if self.is_admin or not self.scopes or scope in self.scopes:
            return
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE,
            f"This API key is not authorized for scope '{scope}'.",
        )


def extract_bearer_token(request: Request) -> str:
    """Accept both OpenAI (`Authorization: Bearer`) and Anthropic (`x-api-key`)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    x_api_key = request.headers.get("x-api-key", "").strip()
    if x_api_key:
        return x_api_key
    raise GatewayError(
        ErrorCode.MISSING_API_KEY,
        "No API key provided. Send 'Authorization: Bearer <key>' or 'x-api-key: <key>'.",
    )


async def authenticate(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Resolve the caller from an API key or a console session.

    Programs send a key; the console sends a cookie. Both end up as the same
    Principal, so every route downstream is written once.
    """
    from_session = await _principal_from_session(request, session)
    if from_session is not None:
        return from_session

    token = extract_bearer_token(request)
    digest = hash_api_key(token)

    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        # Same message for unknown vs malformed: no oracle for key probing.
        raise GatewayError(ErrorCode.INVALID_API_KEY, "Invalid API key.")

    now = utcnow()
    if api_key.revoked_at is not None:
        raise GatewayError(ErrorCode.API_KEY_REVOKED, "This API key has been revoked.")
    if api_key.expires_at is not None and _aware(api_key.expires_at) < now:
        raise GatewayError(ErrorCode.API_KEY_EXPIRED, "This API key has expired.")

    user = await session.get(User, api_key.user_id)
    if user is None or user.status != "active":
        raise GatewayError(
            ErrorCode.ACCOUNT_DISABLED, "This account is not active. Contact your manager."
        )

    role = normalise_role(user.role)
    # เพดานเฉพาะใบอยู่คนละตาราง จึงต้องถาม — แต่ถามเฉพาะเมื่อคำตอบเปลี่ยนอะไรได้:
    # สมาชิกไม่มีอำนาจให้เสีย และใบที่จำกัดโมเดล/ผูก workspace อยู่แล้วก็ถูกนับไปแล้ว ·
    # คำขอของสมาชิก (เกือบทั้งหมดของทราฟฟิก) จึงไม่เสีย query เพิ่มแม้แต่ตัวเดียว
    capped = (
        role in PRIVILEGED_ROLES
        and not limits_on_key(
            models=api_key.models, access_groups=api_key.access_groups,
            workspace_id=api_key.workspace_id,
        )
        and await has_cap_in_force(session, api_key.id)
    )

    # Best-effort last-used stamp; never fail a request over telemetry.
    #
    # ประทับเวลาแบบหยาบ ๆ พอ — เดิมเขียน + commit **ทุก request** ซึ่งเป็น write
    # transaction เต็มตัวหนึ่งรายการต่อหนึ่งคำขอ เพียงเพื่อข้อมูลที่หน้าเว็บแสดงเป็น
    # "ใช้ล่าสุด <วันเวลา>" · บน SQLite ที่ทุก write ต้องรอคิวกัน นี่คือคอขวดตรง ๆ
    # และมันอยู่บน hot path ของทุกคำขอที่ผ่าน gateway
    #
    # ความละเอียดระดับนาทีเกินพอสำหรับสิ่งที่ค่านี้ถูกใช้ทำ · คีย์ที่ถูกยิงถี่ ๆ จึงเขียน
    # จริงแค่นาทีละครั้ง ส่วนคีย์ที่นาน ๆ ใช้ทีก็ยังได้เวลาที่ตรงเหมือนเดิม
    previous = _aware(api_key.last_used_at) if api_key.last_used_at else None
    if previous is None or (now - previous) >= LAST_USED_STAMP_INTERVAL:
        try:
            api_key.last_used_at = now
            await session.commit()
        except Exception:
            await session.rollback()
            log.warning("could not update last_used_at for key %s", api_key.id)

    return Principal(
        user_id=user.id,
        external_id=user.external_id,
        role=role,
        display_name=user.display_name,
        api_key_id=api_key.id,
        workspace_id=api_key.workspace_id,
        scopes=list(api_key.scopes or []),
        key_models=list(api_key.models or []),
        key_access_groups=list(api_key.access_groups or []),
        via="key",
        key_capped=capped,
    )


async def has_cap_in_force(session: AsyncSession, api_key_id: str) -> bool:
    """มีเพดานโควตาเฉพาะใบนี้ที่ด่านโควตายังบังคับอยู่ไหม

    "ยังมีผล" ต้องหมายความอย่างเดียวกับ `QuotaManager.resolve_key_limits`
    (เปิดอยู่ และยังไม่หมดอายุ) — เพดานที่ปิดไปแล้วไม่ได้จำกัดอะไร นับมันคือถอดอำนาจ
    ของใบที่ไม่มีอะไรจำกัดอยู่จริง · tests/test_a_limited_key_has_no_admin_power.py
    เทียบสองที่นี้กันทุกสถานะ

    เทียบเวลาใน Python ไม่ใช่ใน SQL เหมือนที่ quota ทำ: SQLite คืนเวลาแบบไม่มีโซน
    PostgreSQL คืนแบบมีโซน และ `_aware` คือที่เดียวที่ทำให้สองฝั่งเทียบกันได้
    """
    rows = await session.execute(
        select(QuotaPolicy.expires_at).where(
            QuotaPolicy.enabled.is_(True), QuotaPolicy.api_key_id == api_key_id
        )
    )
    now = utcnow()
    return any(cap_still_runs(expires, now) for (expires,) in rows)


def cap_still_runs(expires_at: datetime | None, now: datetime) -> bool:
    """แยกออกมาให้ `scripts/restricted_key_report.py` ใช้ตัวเดียวกัน"""
    return expires_at is None or _aware(expires_at) > now



async def _principal_from_session(
    request: Request, session: AsyncSession
) -> Principal | None:
    """The browser's cookie, if it carries a session this server still honours."""
    from app.core.passwords import read_session, read_session_cookie

    raw = read_session_cookie(request.cookies, request.url.scheme == "https")
    if not raw:
        return None
    payload = read_session(raw)
    if payload is None:
        return None

    user = await session.get(User, payload.get("sub"))
    if user is None or user.status != "active":
        return None
    # A password change bumps session_epoch, which retires every token issued
    # before it without needing a session table.
    if int(payload.get("epoch", -1)) != int(user.session_epoch or 0):
        return None

    return Principal(
        user_id=user.id,
        external_id=user.external_id,
        role=normalise_role(user.role),
        display_name=user.display_name,
        api_key_id="",
        workspace_id=None,
        scopes=[],
        via="session",
    )


async def require_admin(principal: Principal = Depends(authenticate)) -> Principal:
    if not principal.is_admin:
        if principal.role == "admin":
            raise _limited_key(principal, "administrator")
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE, "Administrator privileges are required."
        )
    return principal


async def require_manager(principal: Principal = Depends(authenticate)) -> Principal:
    if not principal.is_manager:
        if principal.role in PRIVILEGED_ROLES:
            raise _limited_key(
                principal, "administrator" if principal.role == "admin" else "manager"
            )
        raise GatewayError(
            ErrorCode.INSUFFICIENT_SCOPE, "Manager privileges are required."
        )
    return principal


def _limited_key(principal: Principal, rights: str) -> GatewayError:
    """คำปฏิเสธของใบที่ *เจ้าของ* มีสิทธิ์ แต่ตัวใบถูกจำกัดไว้

    ข้อความเดิม ("Administrator privileges are required.") จะพาคนไปผิดทาง: เขาเป็น
    ผู้ดูแลจริง แล้วจะไปขอสิทธิ์จากใคร · ต้องบอกว่าอะไรบนใบที่ทำให้เป็นแบบนี้ และทางออก
    สองทางที่เดินได้จริง — สมาชิกธรรมดายังได้ข้อความเดิม เพราะสำหรับเขาข้อความเดิมถูก
    """
    limits = principal.key_limits
    return GatewayError(
        ErrorCode.INSUFFICIENT_SCOPE,
        f"This API key is limited to {_spoken(limits)}, so it does not carry its "
        f"owner's {rights} rights: a key issued for one job must not be able to "
        "lift its own limits or issue other keys. Sign in to the console to do "
        "this, or use a key issued without a model list, access group, workspace "
        "or quota of its own.",
        details={
            "reason_code": "restricted_key",
            "limited_by": list(limits),
            "owner_role": principal.role,
        },
    )


def _spoken(limits: tuple[str, ...]) -> str:
    words = [KEY_LIMIT_WORDS[name] for name in limits]
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


@dataclass(frozen=True)
class Permission:
    """What this caller may call, and the sentence explaining why.

    `aliases is None` means nothing narrows them - not "nothing is allowed",
    which is the reading that would lock out every key issued before workspaces
    were used at all.
    """

    aliases: set[str] | None
    reason: str = ""
    # รหัสของเหตุผลข้างบน สำหรับหน้าเว็บที่ไม่ได้เป็นภาษาอังกฤษ · ประโยคใน `reason`
    # เป็นภาษาอังกฤษเพราะ audit log และคอนโซลผู้ดูแลอ่านมันตรง ๆ — หน้า member เป็น
    # ภาษาไทย เอาประโยคอังกฤษไปแปะกลางประโยคไทยจึงอ่านไม่รู้เรื่อง
    reason_code: str = ""

    def allows(self, alias: str) -> bool:
        return self.aliases is None or alias in self.aliases


UNRESTRICTED = Permission(aliases=None)


async def permitted_aliases(
    session: AsyncSession, principal: Principal, gateway=None
) -> Permission:
    """The one place that decides which models a caller may use.

    Three things can narrow it, and each only ever narrows:

      1. the workspace the key was bound to when it was issued - for as long as
         its owner is still a member of that workspace
      2. otherwise, the workspaces its owner belongs to (union across them)
      3. the alias list written on the key itself

    Membership was bookkeeping until v1.5 - it recorded who was in which class
    and granted nothing - so (2) is gated on `membership_grants_models`, and a
    deployment with keys already in circulation should look at
    `scripts/access_change_report.py` before switching it on.

    (1) carries the same condition. A key issued for CS101 used to keep CS101's
    models after its owner was taken out of CS101: the binding was read and the
    membership was not, so "remove from the workspace" removed a name from a
    list and nothing else, while the endpoint's own description said the key
    would stop by itself. The membership is checked here, on every request,
    rather than by revoking the key when someone is removed - revoking cannot be
    undone, and putting the person back should be all it takes.

    Union, not intersection, in (2): adding somebody to another class must not
    take access away from them, which is the opposite of what "add to group"
    means to everyone who says it out loud.
    """
    scope: set[str] | None = None
    reason = ""
    code = ""

    if principal.workspace_id is not None:
        if membership_counts(principal.role, gateway) and not await is_member(
            session, principal.user_id, principal.workspace_id
        ):
            # Returned as it stands: the list on the key cannot add anything to
            # nothing, and the reason has to say the one thing that is true.
            return Permission(aliases=set(), reason=LEFT_WORKSPACE, reason_code="workspace_left")
        scope = await _workspace_models(session, [principal.workspace_id])
        reason = "the workspace this key was issued for"
        code = "workspace"
    elif membership_counts(principal.role, gateway):
        # Managers are scoped like members: someone who looks after CS101 should
        # not be handing out ART200's models. Admins run the gateway itself and
        # stay unscoped - the alternative is adding them to every workspace,
        # which is a chore with no end and no security value.
        scope = await _models_via_membership(session, principal.user_id)
        if scope is not None:
            reason = "the workspaces you belong to"
            code = "membership"

    # The limits written on the key apply to everyone, admins included. The
    # workspace rules above are about who you are; this one is about what the
    # person issuing the key meant it for - a key made for one script should
    # stay limited even if its owner is later promoted.
    #
    # A named bundle and a hand-written list answer the same question, so they
    # add up with each other before narrowing what the workspaces allowed.
    on_key = set(principal.key_models)
    if principal.key_access_groups:
        on_key |= await _group_models(session, principal.key_access_groups)
    # "Did the issuer write a limit on this key", not "did it expand to
    # anything". A key limited to one bundle expanded to nothing the moment that
    # bundle was switched off, the empty set read as "no limit was written", and
    # the key got the whole catalogue: switching a bundle off - the thing the
    # console offers as the way to stop it granting anything - widened every key
    # limited by it (2026-10-09). An empty result of a limit that was written is
    # an empty allow-list, the same distinction `_models_via_membership` draws
    # between "in no workspace" and "in workspaces that allow nothing".
    if principal.key_models or principal.key_access_groups:
        scope = on_key if scope is None else scope & on_key
        if not on_key:
            # Only bundles were named, and none of them grants anything now.
            return Permission(aliases=set(), reason=BUNDLE_OFF, reason_code="key_bundle_off")
        reason = (
            f"{reason}, and the list on this key" if reason else "the model list on this key"
        )
        code = f"{code}+key" if code else "key"

    return Permission(aliases=scope, reason=reason, reason_code=code)


LEFT_WORKSPACE = (
    "the workspace this key was issued for, which its owner is no longer a member of"
)
BUNDLE_OFF = (
    "the access group this key is limited to, which is switched off or no longer exists"
)


def membership_counts(role: str, gateway=None) -> bool:
    """Does being in a workspace decide anything for somebody with this role?

    Not for an admin: they run the gateway and are unscoped, so a key of theirs
    bound to a workspace is narrowed to its models whether or not they were ever
    enrolled in it. And not on a deployment that set `membership_grants_models:
    false` - there membership is bookkeeping, a bound key keeps the meaning it
    had when it was issued, and the switch stays what it was written to be: the
    way to upgrade without re-permissioning keys already in circulation.

    One function so that the request path, the leave endpoint's report and the
    key-issuing check cannot disagree about who the rule applies to.
    """
    if normalise_role(role) == "admin":
        return False
    return gateway is None or bool(gateway.membership_grants_models)


async def is_member(session: AsyncSession, user_id: str, workspace_id: str) -> bool:
    """One indexed lookup (`uq_enrollment` covers both columns).

    This is a query the bound-key path did not make before. It is the price of
    the rule being true on every request instead of at the moment somebody
    remembers to revoke a key.
    """
    row = await session.execute(
        select(Membership.id)
        .where(Membership.user_id == user_id, Membership.workspace_id == workspace_id)
        .limit(1)
    )
    return row.first() is not None


async def models_via_membership(session: AsyncSession, user_id: str) -> set[str] | None:
    """What somebody's workspaces allow, added together. None = in no workspace."""
    return await _models_via_membership(session, user_id)


async def _group_models(session: AsyncSession, group_ids) -> set[str]:
    """Expand bundles into the aliases they name.

    A disabled bundle expands to nothing rather than being ignored: turning one
    off is meant to take its models away everywhere at once, which is the whole
    reason for putting them in a bundle.
    """
    ids = list(group_ids or [])
    if not ids:
        return set()
    rows = await session.execute(
        select(AccessGroup.models).where(
            AccessGroup.id.in_(ids), AccessGroup.enabled.is_(True)
        )
    )
    return {alias for (models,) in rows for alias in (models or [])}


async def managed_workspaces(
    session: AsyncSession, principal: Principal
) -> set[str] | None:
    """The workspaces this person administers. `None` means all of them.

    A manager manages the classes they are in and nothing else: someone who
    looks after CS101 has no business reading ART200's usage or issuing its
    keys. Admins run the gateway and are not scoped.

    Note the default runs the other way from `permitted_aliases`: a manager in
    no workspace manages nothing, where a member in no workspace may call
    everything. The reason is different in each case. Model access defaults open
    so that a deployment which never adopted workspaces keeps working; there is
    no equivalent history on the admin plane, and defaulting it open would mean
    that promoting somebody to manager silently hands them the whole institution.
    """
    if principal.is_admin:
        return None
    rows = await session.execute(
        select(Membership.workspace_id).where(Membership.user_id == principal.user_id)
    )
    return {row[0] for row in rows}


async def users_in_workspaces(
    session: AsyncSession, workspaces: set[str]
) -> set[str]:
    """Everyone in any of these workspaces."""
    if not workspaces:
        return set()
    rows = await session.execute(
        select(Membership.user_id).where(Membership.workspace_id.in_(workspaces))
    )
    return {row[0] for row in rows}


async def _models_via_membership(session: AsyncSession, user_id: str) -> set[str] | None:
    """One join rather than "which groups" followed by "which models".

    This runs on every request that is not workspace-bound, so the difference
    between one query and two is paid by every call the gateway serves.

    None means the person is in no group at all, which is not the same as being
    in groups that allow nothing: the first is unrestricted, the second is a
    deliberate empty allow-list.
    """
    # The suspension is applied to the *models* side, never to the membership
    # side. Filtering out the membership row would make a suspended workspace
    # read as "this person is in no group", which is the unrestricted case -
    # suspending a class would hand its students the whole catalogue.
    rows = await session.execute(
        select(Membership.workspace_id, WorkspaceModel.model_alias)
        .join(Workspace, Workspace.id == Membership.workspace_id)
        .outerjoin(
            WorkspaceModel,
            (WorkspaceModel.workspace_id == Membership.workspace_id)
            & (WorkspaceModel.enabled.is_(True))
            & (Workspace.status == "active"),
        )
        .where(Membership.user_id == user_id)
    )
    pairs = rows.all()
    if not pairs:
        return None
    aliases = {alias for _, alias in pairs if alias is not None}
    return aliases | await _bundles_of(session, {ws for ws, _ in pairs})


async def _workspace_models(session: AsyncSession, workspaces: list[str]) -> set[str]:
    """A suspended workspace grants nothing.

    `Workspace.status` has existed since the first release and nothing ever read
    it - the same shape of problem as `Membership` before v1.5: a field that
    looks like a switch and is not wired to anything. Suspending a class now
    means what it says, and unlike revoking its members' keys it can be undone.
    """
    rows = await session.execute(
        select(WorkspaceModel.model_alias)
        .join(Workspace, Workspace.id == WorkspaceModel.workspace_id)
        .where(
            WorkspaceModel.workspace_id.in_(workspaces),
            WorkspaceModel.enabled.is_(True),
            Workspace.status == "active",
        )
    )
    return {row[0] for row in rows} | await _bundles_of(session, workspaces)


async def _bundles_of(session: AsyncSession, workspaces) -> set[str]:
    """Aliases the bundles attached to these workspaces expand to.

    Bundles add to the models ticked on the workspace: both answer "what may
    this class call", so they are two ways of writing one list rather than two
    rules that have to be reconciled.
    """
    rows = await session.execute(
        select(AccessGroup.models)
        .join(
            WorkspaceAccessGroup,
            WorkspaceAccessGroup.access_group_id == AccessGroup.id,
        )
        .join(Workspace, Workspace.id == WorkspaceAccessGroup.workspace_id)
        .where(
            WorkspaceAccessGroup.workspace_id.in_(list(workspaces)),
            AccessGroup.enabled.is_(True),
            Workspace.status == "active",
        )
    )
    aliases: set[str] = set()
    for (models,) in rows:
        aliases |= set(models or [])
    return aliases


async def assert_model_permitted(
    session: AsyncSession, principal: Principal, alias: str, gateway=None
) -> None:
    """Workspace policy gate (PRD §15 step 1)."""
    permission = await permitted_aliases(session, principal, gateway)
    if permission.allows(alias):
        return

    if permission.reason_code == "workspace_left":
        # Not "ask for the model": the model list is fine, the membership is
        # what went. Saying so is the difference between a ticket that reads
        # "the gateway is broken" and one that reads "add me back to CS101".
        raise GatewayError(
            ErrorCode.MODEL_NOT_PERMITTED,
            f"'{alias}' is not available to you: this key was issued for a workspace "
            "its owner is no longer a member of. Ask that workspace's manager to add "
            "you back, or use a key that is not tied to it.",
            details={"model": alias, "allowed": [], "reason": permission.reason,
                     "reason_code": permission.reason_code},
        )

    if permission.reason_code == "key_bundle_off":
        # Not "ask for the model" either: nothing is wrong with the key or the
        # person, the bundle it points at was turned off.
        raise GatewayError(
            ErrorCode.MODEL_NOT_PERMITTED,
            f"'{alias}' is not available to you: this key is limited to an access "
            "group that is switched off or no longer exists, so it can call nothing "
            "right now. Ask an administrator to switch the group back on, or to "
            "give this key a model list.",
            details={"model": alias, "allowed": [], "reason": permission.reason,
                     "reason_code": permission.reason_code},
        )

    allowed = sorted(permission.aliases or [])
    raise GatewayError(
        ErrorCode.MODEL_NOT_PERMITTED,
        f"'{alias}' is not available to you. Allowed by {permission.reason}: "
        + (", ".join(allowed) if allowed else "nothing yet — ask your manager."),
        details={"model": alias, "allowed": allowed, "reason": permission.reason},
    )


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; normalize before comparing."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
