"""ที่เก็บคีย์ของผู้ให้บริการปลายทาง ที่ผู้ดูแลตั้งได้จากหน้าเว็บ

เดิม endpoint บอกได้แค่ *ชื่อ* ตัวแปรสภาพแวดล้อม (`api_key_env`) แล้วค่าจริงต้องไป
ตั้งในสภาพแวดล้อมของ process ซึ่งแปลว่าต้องแก้ไฟล์บนเครื่องเซิร์ฟเวอร์แล้ว restart
service · ผู้ดูแลที่ใช้หน้าเว็บอย่างเดียว — ซึ่งคือคนส่วนใหญ่ที่เราส่งมอบให้ — จึงเพิ่ม
ผู้ให้บริการออนไลน์ไม่ได้เลย มีคีย์อยู่ในมือแต่ไม่มีช่องให้กรอก

เหตุผลของการแยกไฟล์ ไม่เอาไปไว้ใน registry:
  * `config/models/*.yaml` อยู่ใน git และถูกส่งต่อระหว่างเครื่อง — คีย์ห้ามไปอยู่ตรงนั้น
  * ทะเบียนถูกอ่านและ render ออกหน้าเว็บทั้งไฟล์ (Preview YAML) คีย์จะโผล่ทันที

กติกาของที่นี่:
  * ไฟล์เดียว โหมด 0600 อยู่นอกสายตา git
  * `os.environ` ชนะเสมอ — เครื่องที่ตั้งผ่าน systemd/.env มาก่อนต้องไม่เปลี่ยนพฤติกรรม
  * ค่าจริงไม่เคยถูกส่งกลับออก API และไม่เคยถูก log — ออกไปได้แค่ "ตั้งไว้แล้วหรือยัง"
  * อ่านใหม่เมื่อไฟล์เปลี่ยน เพราะเกตเวย์รันหลาย worker และคนตั้งค่าลงไปที่ตัวเดียว
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ชื่อเดียวกับกติกาของตัวแปรสภาพแวดล้อม เพื่อให้ย้ายไปตั้งใน systemd ทีหลังได้โดยไม่ต้องแก้อะไร
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretStoreError(ValueError):
    """ชื่อหรือค่าที่รับไม่ได้ — ข้อความอธิบายให้ผู้ใช้อ่านรู้เรื่อง"""


class SecretStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._cache: dict[str, str] = {}
        self._mtime_ns = -1

    # ── อ่าน ────────────────────────────────────────────────────────────────
    def _load(self) -> dict[str, str]:
        """โหลดใหม่เมื่อไฟล์เปลี่ยน · worker ตัวอื่นเป็นคนเขียนก็เห็น"""
        try:
            stat = self._path.stat()
        except OSError:
            self._cache, self._mtime_ns = {}, -1
            return self._cache
        if stat.st_mtime_ns != self._mtime_ns:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
                self._cache = {k: str(v) for k, v in raw.items() if isinstance(k, str)}
            except (OSError, ValueError) as exc:
                # ไฟล์เสียไม่ควรทำให้เกตเวย์ล่ม — ของเดิมในหน่วยความจำยังใช้ต่อได้
                log.error("secret store unreadable (%s): %s", self._path, exc)
                return self._cache
            self._mtime_ns = stat.st_mtime_ns
        return self._cache

    def resolve(self, name: str) -> str:
        """ค่าของคีย์ชื่อนี้ · สภาพแวดล้อมของ process มาก่อนเสมอ

        ลำดับนี้จงใจ: เครื่องที่ตั้งคีย์ผ่าน systemd หรือ .env ไว้อยู่แล้วต้องทำงานเหมือนเดิม
        ทุกประการ และผู้ดูแลระบบยังใช้วิธีนั้นแทนได้ถ้าไม่อยากให้คีย์อยู่ในไฟล์ของแอป
        """
        if not name:
            return ""
        from_env = os.environ.get(name, "")
        return from_env or self._load().get(name, "")

    def is_set(self, name: str) -> bool:
        return bool(self.resolve(name))

    def stored_names(self) -> list[str]:
        """ชื่อคีย์ที่เก็บไว้ที่นี่ — ชื่อเท่านั้น ไม่มีค่า"""
        return sorted(self._load())

    def status(self, names: list[str]) -> list[dict[str, Any]]:
        """ตั้งไว้หรือยัง และตั้งไว้ที่ไหน — ไม่มีค่าจริงอยู่ในผลลัพธ์"""
        stored = self._load()
        out = []
        for name in dict.fromkeys(n for n in names if n):
            in_env = bool(os.environ.get(name, ""))
            out.append({
                "name": name,
                "set": in_env or bool(stored.get(name)),
                # ผู้ดูแลต้องรู้ว่าแก้ที่หน้าเว็บแล้วจะมีผลไหม — ถ้าค่าถูกบังด้วย env
                # การกดบันทึกที่นี่จะไม่เปลี่ยนอะไร และนั่นคือเรื่องที่ต้องบอกกันตรง ๆ
                "source": "env" if in_env else ("stored" if stored.get(name) else ""),
            })
        return out

    # ── เขียน ───────────────────────────────────────────────────────────────
    def set(self, name: str, value: str) -> None:
        name, value = name.strip(), value.strip()
        if not NAME.match(name):
            raise SecretStoreError(
                "ชื่อคีย์ต้องเป็นรูปแบบตัวแปรสภาพแวดล้อม เช่น MINIMAX_API_KEY"
            )
        if not value:
            raise SecretStoreError("ค่าว่าง — ถ้าต้องการเอาออกให้ใช้การลบ")
        data = dict(self._load())
        data[name] = value
        self._write(data)

    def delete(self, name: str) -> bool:
        data = dict(self._load())
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    def _write(self, data: dict[str, str]) -> None:
        """เขียนแบบ atomic และตั้งสิทธิ์ *ก่อน* ที่ค่าจะลงดิสก์

        mkstemp ให้ 0600 มาตั้งแต่แรกอยู่แล้ว จึงไม่มีจังหวะที่ไฟล์อ่านได้ทั้งเครื่อง
        แม้ชั่วครู่ · ตัวโฟลเดอร์ก็ปิดด้วยเช่นกัน เพราะรายชื่อคีย์เองก็ไม่ใช่ของสาธารณะ
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.parent.chmod(0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".secrets-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._mtime_ns = -1  # บังคับให้รอบหน้าอ่านของที่เพิ่งเขียน
