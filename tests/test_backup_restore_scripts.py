"""backup → restore ต้องได้เกตเวย์ที่ยิง upstream ได้จริง ไม่ใช่แค่เปิดขึ้น

เคสจริง 2026-10-06: `backup.sh` เก็บฐานข้อมูล · `config/` · `.env` แล้วรายงานว่าครบ แต่ **ไม่เก็บ
`data/secrets.json`** — ที่เดียวที่มีคีย์ของผู้ให้บริการปลายทางที่ผู้ดูแลกรอกจากคอนโซล (ทะเบียนเก็บแค่
*ชื่อ* ใน `api_key_env`) · restore แล้วสมาชิก คีย์ และทะเบียนกลับมาครบตามที่สคริปต์บอกให้เช็ค
ส่วน upstream ทุกตัวที่ใช้คีย์จากไฟล์นั้นตอบ 401 และกู้จาก archive ไม่ได้ · เครื่องจริงมีไฟล์นี้อยู่

เทสรันสคริปต์จริงทั้งสองตัวกับ install ปลอมใน tmp แล้วดูของที่ได้กลับมา ไม่ได้อ่านข้อความในสคริปต์
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("sqlite3")), reason="ต้องมี bash และ sqlite3"
)

PROVIDER_KEYS = {"MINIMAX_API_KEY": "mm-live-key", "OPENAI_API_KEY": "sk-live-key"}


def make_install(root: Path, *, with_secrets: bool = True) -> Path:
    """install ขั้นต่ำที่ backup.sh ยอมรับ — สคริปต์หา ROOT จากตำแหน่งของตัวเอง จึงต้องคัดลอกไปด้วย"""
    (root / "scripts").mkdir(parents=True)
    for name in ("backup.sh", "restore.sh"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    (root / "data").mkdir()
    db = sqlite3.connect(root / "data" / "gateway.db")
    db.executescript(
        "create table users(id integer primary key, email text);"
        "create table api_keys(id integer primary key, user_id integer);"
        "insert into users(email) values ('a@example.com');"
        "insert into api_keys(user_id) values (1);"
    )
    db.commit()
    db.close()
    (root / "config" / "models").mkdir(parents=True)
    (root / "config" / "models" / "m1.yaml").write_text("alias: m1\n", encoding="utf-8")
    (root / ".env").write_text("GW_API_KEY_PEPPER=pepper-of-this-install\n", encoding="utf-8")
    if with_secrets:
        secrets = root / "data" / "secrets.json"
        secrets.write_text(json.dumps(PROVIDER_KEYS), encoding="utf-8")
        secrets.chmod(0o600)
    return root


def run(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )


def restore(install: Path, archive: Path, into: Path) -> subprocess.CompletedProcess:
    return run(install / "scripts" / "restore.sh", str(archive), "--into", str(into), cwd=install)


def backup(install: Path) -> Path:
    done = run(install / "scripts" / "backup.sh", "--out", str(install / "backups"), cwd=install)
    assert done.returncode == 0, done.stderr
    archives = sorted((install / "backups").glob("litegate-*.tar.gz"))
    assert len(archives) == 1
    return archives[0]


def test_provider_keys_survive_a_backup_and_restore(tmp_path):
    install = make_install(tmp_path / "live")
    archive = backup(install)

    rehearsal = tmp_path / "rehearsal"
    done = restore(install, archive, rehearsal)
    assert done.returncode == 0, done.stderr

    restored = rehearsal / "data" / "secrets.json"
    assert restored.is_file(), "restore ต้องคืนคีย์ของ provider มาด้วย ไม่งั้น upstream ตอบ 401 ทั้งหมด"
    assert json.loads(restored.read_text(encoding="utf-8")) == PROVIDER_KEYS
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600, "ไฟล์นี้คือความลับ — ต้องไม่เปิดให้คนอื่นอ่าน"
    # ของเดิมที่ restore เคยคืนอยู่แล้วต้องยังครบ
    assert (rehearsal / "data" / "gateway.db").is_file()
    assert (rehearsal / "config" / "models" / "m1.yaml").is_file()
    assert "pepper-of-this-install" in (rehearsal / ".env").read_text(encoding="utf-8")


def test_restore_keeps_a_copy_of_the_keys_it_replaces(tmp_path):
    """ฐานข้อมูลกับ config ถูกเก็บสำเนาก่อนทับอยู่แล้ว — คีย์ของ provider ต้องได้การคุ้มครองเท่ากัน"""
    install = make_install(tmp_path / "live")
    archive = backup(install)
    target = tmp_path / "target"
    (target / "data").mkdir(parents=True)
    (target / "data" / "secrets.json").write_text(json.dumps({"OLD_KEY": "old"}), encoding="utf-8")

    done = restore(install, archive, target)
    assert done.returncode == 0, done.stderr
    now = json.loads((target / "data" / "secrets.json").read_text(encoding="utf-8"))
    assert now == PROVIDER_KEYS
    kept = list((target / "data").glob("secrets.json.before-restore-*"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8")) == {"OLD_KEY": "old"}


def test_an_install_without_console_keys_still_backs_up_and_says_so(tmp_path):
    """ไม่เคยกรอกคีย์จากคอนโซล = ไม่มีไฟล์ — ต้องไม่ล้ม และต้องบอกว่าไม่มี ไม่ใช่เงียบ"""
    install = make_install(tmp_path / "live", with_secrets=False)
    done = run(install / "scripts" / "backup.sh", "--out", str(install / "backups"), cwd=install)
    assert done.returncode == 0, done.stderr
    line = next(ln for ln in done.stdout.splitlines() if ln.strip().startswith("secrets"))
    assert "none" in line
    archive = next((install / "backups").glob("litegate-*.tar.gz"))
    with tarfile.open(archive) as tar:
        assert not [n for n in tar.getnames() if n.endswith("secrets.json")]

    rehearsal = tmp_path / "rehearsal"
    done = restore(install, archive, rehearsal)
    assert done.returncode == 0, done.stderr
    assert not (rehearsal / "data" / "secrets.json").exists()


def test_an_unreadable_key_file_stops_the_backup(tmp_path):
    """ไฟล์มีอยู่แต่อ่านไม่ได้ (0600 ของผู้ใช้อื่น) — archive ที่ขาดมันแล้วรายงานสำเร็จคือบั๊กเดิมในรูปใหม่"""
    if os.geteuid() == 0:
        pytest.skip("root อ่านได้ทุกไฟล์")
    install = make_install(tmp_path / "live")
    (install / "data" / "secrets.json").chmod(0o000)
    try:
        done = run(
            install / "scripts" / "backup.sh", "--out", str(install / "backups"), cwd=install
        )
    finally:
        (install / "data" / "secrets.json").chmod(0o600)
    assert done.returncode != 0
    assert not list((install / "backups").glob("litegate-*.tar.gz")), "ห้ามมี archive ที่ขาดไฟล์นี้"


# ── secret ที่ผนึกสำเนา API key ──────────────────────────────────────────────

def restore_in_place(install: Path, archive: Path, **exported: str) -> subprocess.CompletedProcess:
    """restore ทับ install เดิม · ตอบคำถามยืนยันของสคริปต์ทาง stdin เหมือนคนพิมพ์

    `exported` = ตัวแปรที่ shell ของผู้ดูแล export ไว้ (สคริปต์อ่านจากสภาพแวดล้อมก่อน .env)
    """
    return subprocess.run(
        ["bash", str(install / "scripts" / "restore.sh"), str(archive), "--in-place"],
        cwd=install, input="restore\n", capture_output=True, text=True, timeout=60,
        # เครื่องที่รันเทสต้องไม่มีผลกับเทส — เหลือเฉพาะที่เทสตั้งเอง
        env={**{k: v for k, v in os.environ.items() if not k.startswith("GW_")}, **exported},
    )


def set_env(install: Path, **values: str) -> None:
    lines = ["GW_API_KEY_PEPPER=pepper-of-this-install"]
    lines += [f"{name}={value}" for name, value in values.items()]
    (install / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_restoring_a_backup_from_before_a_secret_change_says_what_to_keep(tmp_path):
    """backup ก่อนเปลี่ยน GW_KEY_REVEAL_SECRET → restore ทับหลังเปลี่ยน

    restore แบบ --in-place ไม่แตะ .env ของเครื่อง ฐานที่ได้กลับมาจึงผนึกด้วย secret ที่เครื่องนี้
    ไม่มีแล้ว · เดิมสคริปต์รายงานว่า ".env ตรงกับ backup" ทั้งที่เทียบแค่ pepper แล้วผู้ดูแลไปรู้
    ตอนกด Reveal ไม่ได้ · key ทุกใบยังใช้งานได้ จึงเป็นคำเตือน ไม่ใช่การปฏิเสธ
    """
    install = make_install(tmp_path / "live")
    set_env(install, GW_KEY_REVEAL_SECRET="old-reveal-secret-not-real")
    archive = backup(install)
    set_env(install, GW_KEY_REVEAL_SECRET="new-reveal-secret-not-real")

    done = restore_in_place(install, archive)
    assert done.returncode == 0, done.stderr
    said = done.stdout + done.stderr
    assert "WARNING" in said and "GW_KEY_REVEAL_SECRET_PREVIOUS" in said
    assert "reseal" in said, "บอกขั้นถัดไปด้วย ไม่ใช่แค่บอกว่ามีปัญหา"
    assert "old-reveal-secret-not-real" not in said and "new-reveal-secret-not-real" not in said
    assert "already matches the backup" not in said, "เทียบแค่ pepper — ห้ามบอกว่า .env ตรงทั้งไฟล์"
    # .env ของเครื่องต้องไม่ถูกแตะ — secret ปัจจุบันยังเป็นตัวใหม่
    assert "new-reveal-secret-not-real" in (install / ".env").read_text(encoding="utf-8")


def test_restoring_under_the_same_reveal_secret_says_nothing_about_it(tmp_path):
    """ยามที่ร้องทุกครั้งคือยามที่ไม่มีใครฟัง"""
    install = make_install(tmp_path / "live")
    set_env(install, GW_KEY_REVEAL_SECRET="same-reveal-secret-not-real")
    archive = backup(install)

    done = restore_in_place(install, archive)
    assert done.returncode == 0, done.stderr
    assert "GW_KEY_REVEAL_SECRET" not in done.stdout + done.stderr


def test_restoring_when_the_backups_secret_is_the_live_previous_one_is_not_alarming(tmp_path):
    """ผู้ดูแลทำตามขั้นตอนแล้ว (ใส่ secret ของ backup เป็น PREVIOUS) — บอกแค่ว่าต้องผนึกใหม่อีกรอบ"""
    install = make_install(tmp_path / "live")
    set_env(install, GW_KEY_REVEAL_SECRET="old-reveal-secret-not-real")
    archive = backup(install)
    set_env(install, GW_KEY_REVEAL_SECRET="new-reveal-secret-not-real",
            GW_KEY_REVEAL_SECRET_PREVIOUS="old-reveal-secret-not-real")

    done = restore_in_place(install, archive)
    assert done.returncode == 0, done.stderr
    said = done.stdout + done.stderr
    assert "WARNING" not in said
    assert "NOTE" in said and "reseal" in said


SAME = "same-secret-not-real"
REVEAL = "GW_KEY_REVEAL_SECRET"


@pytest.mark.parametrize("in_backup, live_file, live_shell", [
    # .env ของ backup ใส่เครื่องหมายคำพูด · shell ของผู้ดูแล export ค่าเดียวกันแบบไม่มี
    (f'{REVEAL}="{SAME}"', None, SAME),
    # สองไฟล์เขียนค่าเดียวกันคนละแบบ
    (f"{REVEAL}={SAME}", f"{REVEAL}='{SAME}'", None),
    # ไฟล์ที่ถูกแก้บน Windows — มี \r ท้ายบรรทัด
    (f"{REVEAL}={SAME}\r", f"{REVEAL}={SAME}", None),
    # ช่องว่างท้ายบรรทัดที่มองไม่เห็น
    (f"{REVEAL}={SAME}  ", f"{REVEAL}={SAME}", None),
], ids=["quoted-file-vs-exported", "quoted-vs-bare", "crlf", "trailing-space"])
def test_the_same_secret_written_differently_is_still_the_same_secret(
        tmp_path, in_backup, live_file, live_shell):
    """ผู้ตรวจอิสระ 2026-10-09: สคริปต์เทียบ *ข้อความดิบ* ของบรรทัดใน .env กับค่าที่ shell export

    systemd (EnvironmentFile) และ pydantic-settings ถอดเครื่องหมายคำพูดออกทั้งคู่ เกตเวย์จึงเห็น
    ค่าเดียวกัน — แต่สคริปต์เห็นว่าต่าง แล้วเตือนให้ผู้ดูแลไปตั้ง PREVIOUS และผนึกใหม่โดยไม่มีเหตุ
    คำเตือนที่ผิดสอนให้คนเลิกอ่านคำเตือน
    """
    pepper = "GW_API_KEY_PEPPER=pepper-of-this-install\n"
    install = make_install(tmp_path / "live")
    (install / ".env").write_text(pepper + in_backup + "\n", encoding="utf-8")
    archive = backup(install)
    (install / ".env").write_text(pepper + (live_file + "\n" if live_file else ""),
                                  encoding="utf-8")

    exported = {"GW_KEY_REVEAL_SECRET": live_shell} if live_shell else {}
    done = restore_in_place(install, archive, **exported)
    assert done.returncode == 0, done.stderr
    assert "GW_KEY_REVEAL_SECRET" not in done.stdout + done.stderr


def test_a_quoted_secret_that_really_differs_still_warns(tmp_path):
    """การถอดเครื่องหมายคำพูดต้องไม่ทำให้ค่าที่ต่างกันจริงเงียบไปด้วย"""
    install = make_install(tmp_path / "live")
    set_env(install, GW_KEY_REVEAL_SECRET='"old-reveal-secret-not-real"')
    archive = backup(install)
    set_env(install, GW_KEY_REVEAL_SECRET="'new-reveal-secret-not-real'")

    done = restore_in_place(install, archive)
    assert done.returncode == 0, done.stderr
    assert "WARNING" in done.stdout + done.stderr
