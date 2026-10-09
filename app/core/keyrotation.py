"""เปลี่ยน secret ที่ผนึกสำเนา API key: ดูว่าของอยู่ใต้ตัวไหน แล้วย้ายเมื่อมีคนสั่ง

`app/core/keyvault.py` รู้แค่วิธีผนึกกับเปิดทีละใบ · ไฟล์นี้คือส่วนที่มองทั้งฐาน:

  * `survey`  — นับว่าสำเนาแต่ละใบเปิดได้ด้วย secret ตัวไหน ใบไหนเปิดไม่ได้ และมีอะไร
    ในการตั้งค่าที่ควรเตือน · อ่านอย่างเดียว เรียกตอนเริ่ม process และจากหน้าสรุป
  * `reseal`  — ย้ายใบที่ยังอยู่ใต้ secret เก่ามาอยู่ใต้ตัวปัจจุบัน · เขียนฐาน

ทำไม `reseal` ไม่รันเองตอนเริ่ม process (แบบที่ OrcaRouter-Lite ทำกับคีย์ของ provider):

  1. เกตเวย์จริงรัน uvicorn 4 worker ที่เริ่มพร้อมกัน อะไรที่ทำ "ตอนเริ่ม" จึงรันสี่ครั้ง
     ซ้อนกัน · เขียนให้ปลอดภัยได้ (ดู `reseal`) แต่สามในสี่รอบคืองานเปล่าที่แย่งล็อกของ
     SQLite กับ bootstrap ของ worker อื่น ในจังหวะที่ระบบกำลังพยายามขึ้น
  2. การย้ายของที่ผนึกไปอยู่ใต้ secret อีกตัวคือการเปลี่ยนท่าทีความปลอดภัย: หลังจากนั้น
     ตัวเก่าเปิดอะไรไม่ได้อีก และ backup ก่อนหน้านั้นต้องใช้ secret คนละตัวกับฐานปัจจุบัน
     เรื่องแบบนี้ควรเป็นสิ่งที่ *มีคนทำ* — มีชื่อและเวลาใน audit log — ไม่ใช่สิ่งที่เกิดขึ้น
     เพราะ service ถูก restart · หลักเดียวกับที่ `keyvault` ใช้ตอนตัดสินว่าฟีเจอร์ต้องปิดไว้ก่อน
  3. ค่าที่พิมพ์ผิดใน `.env` ไม่ควรมีผลถาวรทันทีที่ restart · ตอนเริ่มจึงแค่ *รายงาน*
     ให้ผู้ดูแลเห็นตัวเลขก่อน แล้วค่อยกดเอง

ระหว่างที่ยังไม่ได้กด ไม่มีอะไรเสีย: ใบที่อยู่ใต้ตัวเก่าเปิดได้ตามปกติตราบที่ยังตั้ง
`GW_KEY_REVEAL_SECRET_PREVIOUS` อยู่ และ log ตอนเริ่มกับหน้าสรุปบอกจำนวนที่ค้างทุกครั้ง
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import keyvault
from app.db.models import ApiKey, AuditLog

log = logging.getLogger(__name__)

RESEAL_ACTION = "keyvault.reseal"


class ResealRefused(ValueError):
    """สั่งผนึกใหม่ในสภาพที่ทำไม่ได้ — ข้อความอธิบายให้ผู้ดูแลอ่านรู้เรื่อง"""


@dataclass
class Survey:
    enabled: bool
    current_key_id: str | None
    previous_key_id: str | None
    counts: dict[str, int]
    # ใบที่เปิดไม่ได้ — พอให้ผู้ดูแลชี้ตัวได้จากสิ่งที่เห็นบนจอ ไม่มีตัว key และไม่มีค่าที่ผนึก
    lost: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    @property
    def sealed(self) -> int:
        return sum(self.counts.values())

    @property
    def needs_attention(self) -> bool:
        """มีงานค้างหรือมีของที่เปิดไม่ได้ไหม — CLI ใช้ตัดสิน exit code"""
        return bool(self.counts[keyvault.PREVIOUS] or self.counts[keyvault.LOST])

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "current_key_id": self.current_key_id,
            "previous_key_id": self.previous_key_id,
            "sealed": self.sealed,
            "counts": dict(self.counts),
            "lost": list(self.lost),
            "warnings": list(self.warnings),
        }


@dataclass
class ResealResult:
    resealed: int = 0
    already_current: int = 0
    lost: int = 0
    # แถวที่ถูกเปลี่ยนไประหว่างที่เราอ่านกับตอนที่จะเขียน — มีคนอื่นย้ายไปแล้ว ปล่อยไว้
    changed_meanwhile: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "resealed": self.resealed,
            "already_current": self.already_current,
            "lost": self.lost,
            "changed_meanwhile": self.changed_meanwhile,
        }


def _sealed_rows():
    # ว่างกับ NULL คือ "ไม่มีสำเนา" ทั้งคู่ — แถวที่มีมาก่อนคอลัมน์นี้เป็น NULL
    return ApiKey.key_sealed.is_not(None), ApiKey.key_sealed != ""


def _warnings(counts: dict[str, int], live_lost: int) -> list[dict[str, str]]:
    """สิ่งที่ผู้ดูแลควรรู้เกี่ยวกับการตั้งค่า — แต่ละข้อบอกว่าเกิดอะไรและให้ทำอะไร

    ทุกข้อคือจังหวะที่พลาดได้จริงระหว่างเปลี่ยน secret และเดิมจบที่อาการเดียวกันหมดคือ
    "กด Reveal แล้วไม่ได้" · ไม่มีข้อไหนใส่ secret หรือป้ายของมันลงในข้อความ เพราะข้อความ
    ชุดนี้ถูกเขียนลง log ด้วย
    """
    out: list[dict[str, str]] = []

    def warn(code: str, level: str, message: str) -> None:
        out.append({"code": code, "level": level, "message": message})

    enabled = keyvault.reveal_enabled()
    has_previous = keyvault.previous_configured()
    pending, lost, off = counts[keyvault.PREVIOUS], counts[keyvault.LOST], counts[keyvault.OFF]

    if not enabled:
        if has_previous:
            warn("previous_without_current", "warning",
                 "GW_KEY_REVEAL_SECRET_PREVIOUS is set but GW_KEY_REVEAL_SECRET is not. The "
                 "previous secret is only used while changing secrets and never switches "
                 "key reveal on, so no sealed copy can be opened. Put the secret you want "
                 "to use in GW_KEY_REVEAL_SECRET and restart.")
        if off:
            warn("sealed_but_disabled", "warning",
                 f"{off} sealed key copies are stored, but GW_KEY_REVEAL_SECRET is unset: "
                 "none of them can be revealed and new keys keep no copy. The copies are "
                 "still in the database. Set GW_KEY_REVEAL_SECRET back to the secret that "
                 "sealed them and restart to open them again.")
        return out

    if keyvault.previous_same_as_current():
        warn("previous_equals_current", "warning",
             "GW_KEY_REVEAL_SECRET_PREVIOUS has the same value as GW_KEY_REVEAL_SECRET, so "
             "it does nothing. To change the secret, GW_KEY_REVEAL_SECRET must hold the "
             "NEW secret and GW_KEY_REVEAL_SECRET_PREVIOUS the old one. Otherwise remove "
             "GW_KEY_REVEAL_SECRET_PREVIOUS and restart.")
    elif has_previous and not pending:
        if lost:
            warn("previous_does_not_match", "warning",
                 "GW_KEY_REVEAL_SECRET_PREVIOUS is set, but it opens none of the sealed "
                 "copies that the current secret cannot open - it is not the secret that "
                 "sealed them. Compare the key id each lost copy needs against "
                 "`python -m app.tools keyvault key-id` for the secrets you still hold.")
        else:
            warn("previous_unused", "info",
                 "GW_KEY_REVEAL_SECRET_PREVIOUS is set, but no sealed copy needs it any "
                 "more. Remove it and restart. Keep the old secret somewhere safe for as "
                 "long as you keep backups made before the re-seal: a database restored "
                 "from one of them is sealed under the old secret again.")

    if pending:
        warn("rotation_pending", "warning",
             f"{pending} sealed key copies still open only under "
             "GW_KEY_REVEAL_SECRET_PREVIOUS. Re-seal them (console: Access & Keys > API keys "
             "> Re-seal, or `python -m app.tools keyvault reseal`) before removing the "
             "previous secret - removing it first makes them unreadable.")
    if lost:
        warn("lost", "error",
             f"{lost} sealed key copies open under neither secret ({live_lost} of them "
             "belong to keys that are not revoked) and cannot be revealed. The keys "
             "themselves still work. If you still hold the secret that sealed them, set "
             "it as GW_KEY_REVEAL_SECRET_PREVIOUS, restart, and re-seal; if it is gone, "
             "issue a new key to whoever needs theirs shown again.")
    return out


async def survey(session: AsyncSession) -> Survey:
    """นับสถานะของสำเนาทุกใบ · อ่านอย่างเดียว เรียกซ้ำกี่ครั้งก็ได้

    ทุกใบถูกเปิดจริง (ดู `keyvault.inspect`) แล้วทิ้งผลทันที — เก็บแค่สถานะ
    """
    rows = (await session.execute(
        select(ApiKey.id, ApiKey.name, ApiKey.key_prefix, ApiKey.user_id,
               ApiKey.revoked_at, ApiKey.key_sealed)
        .where(*_sealed_rows())
        .order_by(ApiKey.created_at, ApiKey.id)
    )).all()

    counts = {keyvault.CURRENT: 0, keyvault.PREVIOUS: 0, keyvault.LOST: 0, keyvault.OFF: 0}
    lost: list[dict[str, Any]] = []
    for key_id, name, prefix, user_id, revoked_at, sealed in rows:
        opened = keyvault.inspect(sealed)
        counts[opened.state] += 1
        if opened.state == keyvault.LOST:
            lost.append({
                "id": key_id,
                "name": name or "",
                "key_prefix": prefix,
                "user_id": user_id,
                "revoked": revoked_at is not None,
                "reason": opened.reason,
                # ป้ายของ secret ที่ผนึกใบนี้ — เอาไปเทียบกับ secret เก่าที่เก็บไว้ได้
                "sealed_key_id": opened.key_id,
            })

    current_id, previous_id = keyvault.key_ids()
    live_lost = sum(1 for row in lost if not row["revoked"])
    return Survey(
        enabled=keyvault.reveal_enabled(),
        current_key_id=current_id,
        previous_key_id=previous_id,
        counts=counts,
        lost=lost,
        warnings=_warnings(counts, live_lost),
    )


def log_survey(found: Survey) -> None:
    """หนึ่งบรรทัดนับ แล้วตามด้วยคำเตือนถ้ามี — ไม่มี secret · ป้าย · หรือส่วนใดของ key

    ป้ายของ secret กับ prefix ของ key จงใจไม่อยู่ที่นี่: log มักถูกส่งต่อและเก็บนานกว่า
    ฐานข้อมูล ใครอยากรู้ว่าใบไหนให้ดูหน้าสรุปหรือ `python -m app.tools keyvault status`
    """
    if not found.sealed and not found.warnings:
        return                      # ไม่เคยใช้ฟีเจอร์นี้ — ไม่มีอะไรให้พูด
    log.info(
        "key vault: %d sealed key copies - current=%d previous=%d lost=%d off=%d (reveal %s)",
        found.sealed, found.counts[keyvault.CURRENT], found.counts[keyvault.PREVIOUS],
        found.counts[keyvault.LOST], found.counts[keyvault.OFF],
        "on" if found.enabled else "off",
    )
    levels = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
    for warning in found.warnings:
        log.log(levels.get(warning["level"], logging.WARNING),
                "key vault (sealed key copies): %s", warning["message"])


async def last_reseal(session: AsyncSession) -> dict[str, Any] | None:
    """ผนึกใหม่ครั้งล่าสุด: ใคร เมื่อไร กี่ใบ — บันทึกที่ไม่มีใครอ่านได้ไม่ใช่การบันทึก"""
    row = (await session.execute(
        select(AuditLog).where(AuditLog.action == RESEAL_ACTION)
        .order_by(AuditLog.ts.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    payload = row.payload or {}
    return {"at": row.ts.isoformat(), "by": row.actor_user_id,
            "via": payload.get("via", "console"), "resealed": payload.get("resealed", 0)}


async def record_reseal_from_cli(session: AsyncSession, done: ResealResult) -> None:
    """บันทึกการผนึกใหม่ที่สั่งจากบรรทัดคำสั่ง — ไม่มีคำขอ HTTP และไม่มีผู้ใช้ของเกตเวย์

    คนที่รันคือคนที่เข้าเครื่องได้ จึงจดชื่อผู้ใช้ของระบบปฏิบัติการไว้แทน · ไม่จดก็เท่ากับ
    มีทางเปลี่ยนท่าทีความปลอดภัยที่ไม่ทิ้งร่องรอย ซึ่งขัดกับเหตุผลที่ไม่ให้มันรันเองตั้งแต่แรก
    """
    import getpass

    try:
        os_user = getpass.getuser()
    except Exception:  # noqa: BLE001 - ไม่มีชื่อผู้ใช้ (container บางแบบ) ไม่ใช่เหตุให้ไม่จด
        os_user = ""
    session.add(AuditLog(
        actor_user_id=None,
        action=RESEAL_ACTION,
        target_type="keyvault",
        target_id="",
        payload={**done.as_dict(), "via": "cli", "os_user": os_user[:64]},
        ip="",
    ))
    await session.commit()


async def reseal(session: AsyncSession, result: ResealResult | None = None) -> ResealResult:
    """ย้ายทุกใบที่ยังเปิดได้ด้วย secret เก่าเท่านั้น มาผนึกใต้ตัวปัจจุบัน

    ต้องรันซ้ำได้ ถูกตัดกลางทางได้ และมีสองตัวรันพร้อมกันได้ โดยไม่มีแถวไหนหาย:

      * **commit ทีละแถว** — ถูกตัดตรงไหน แถวก่อนหน้านั้นอยู่ใต้ตัวใหม่ถาวรแล้ว แถวที่เหลือ
        ยังอยู่ใต้ตัวเก่าครบ ไม่มีแถวครึ่ง ๆ กลาง ๆ และทั้งสองแบบเปิดได้ตราบที่ยังตั้ง
        secret ทั้งสองตัวอยู่
      * **เขียนเฉพาะเมื่อแถวยังเป็นค่าที่อ่านมา** (`WHERE key_sealed = ค่าเดิม`) — ถ้ามีคนย้าย
        ไปก่อนแล้ว UPDATE ไม่โดนแถวไหน เราก็ข้าม · ใช้ค่าที่ผนึกเองเป็นตัวเทียบเพราะมัน
        เปลี่ยนทุกครั้งที่ผนึก (nonce ใหม่) และเทียบได้เหมือนกันทั้ง SQLite กับ PostgreSQL
        แนวคิดเดียวกับ `_cas_reencrypt_provider_key` ของ OrcaRouter-Lite
      * **เปิดของที่เพิ่งผนึกดูก่อนเขียน** — ค่าที่เราเปิดคืนไม่ได้จะไม่มีวันถูกเขียนทับค่าที่
        ยังเปิดได้อยู่
      * แถวที่เปิดได้ด้วยตัวปัจจุบันอยู่แล้วไม่ถูกแตะ รอบที่สองจึงไม่เขียนอะไรเลย
      * แถวที่เปิดไม่ได้สักตัวไม่ถูกแตะเช่นกัน — วันหนึ่งอาจมีคนหา secret เดิมเจอ

    ใบที่เพิกถอนแล้วก็ย้ายด้วย: สำเนายังอยู่ในฐาน ถ้าข้ามไป ตัวนับ "รอผนึกใหม่" จะไม่มีวัน
    เป็นศูนย์ และผู้ดูแลจะไม่รู้ว่าเอา secret เก่าออกได้เมื่อไร

    ผู้เรียกเป็นคนเปิด session · ฟังก์ชันนี้ commit เองหลังทุกแถว · ส่ง `result` เข้ามาเองได้
    เมื่อต้องการรู้ว่าย้ายไปแล้วกี่แถวแม้งานจะล้มกลางทาง (ตัวนับถูกบวกหลัง commit ของแถวนั้น)
    """
    if not keyvault.reveal_enabled():
        raise ResealRefused(
            "GW_KEY_REVEAL_SECRET is not set, so there is no current secret to re-seal "
            "under. Set it (to the new secret), keep the old one in "
            "GW_KEY_REVEAL_SECRET_PREVIOUS, restart, then re-seal."
        )

    rows = (await session.execute(
        select(ApiKey.id, ApiKey.key_sealed).where(*_sealed_rows())
        .order_by(ApiKey.created_at, ApiKey.id)
    )).all()
    # ปิด transaction ที่ใช้อ่าน ก่อนเริ่มเขียนทีละแถว — ไม่ถือ snapshot ค้างข้ามทั้งงาน
    await session.commit()

    result = result if result is not None else ResealResult()
    for key_id, observed in rows:
        opened = keyvault.inspect(observed)
        if opened.state == keyvault.CURRENT:
            result.already_current += 1
            continue
        if opened.state != keyvault.PREVIOUS or opened.plaintext is None:
            result.lost += 1
            continue

        fresh = keyvault.seal(opened.plaintext)
        check = keyvault.inspect(fresh)
        if check.state != keyvault.CURRENT or check.plaintext != opened.plaintext:
            # ไม่ควรเกิดได้ — แต่ถ้าเกิด การหยุดดีกว่าการเขียนของที่เปิดไม่ได้ทับของที่เปิดได้
            raise RuntimeError("re-sealed value did not open under the current secret")

        won = await session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.key_sealed == observed)
            .values(key_sealed=fresh)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if won.rowcount:
            result.resealed += 1
        else:
            result.changed_meanwhile += 1
    return result
