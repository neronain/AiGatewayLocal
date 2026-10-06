"""backup → restore ต้องได้เกตเวย์ที่ยิง upstream ได้จริง ไม่ใช่แค่เปิดขึ้น

เคสจริง 2026-10-06: `backup.sh` เก็บฐานข้อมูล · `config/` · `.env` แล้วรายงานว่าครบ แต่ **ไม่เก็บ
`data/secrets.json`** — ที่เดียวที่มีคีย์ของผู้ให้บริการปลายทางที่ผู้ดูแลกรอกจากคอนโซล (ทะเบียนเก็บแค่
*ชื่อ* ใน `api_key_env`) · restore แล้วสมาชิก คีย์ และทะเบียนกลับมาครบตามที่สคริปต์บอกให้เช็ค
ส่วน upstream ทุกตัวที่ใช้คีย์จากไฟล์นั้นตอบ 401 และกู้จาก archive ไม่ได้ · เครื่องจริงมีไฟล์นี้อยู่

เทสรันสคริปต์จริงทั้งสองตัวกับ install ปลอมใน tmp แล้วดูของที่ได้กลับมา ไม่ได้อ่านข้อความในสคริปต์
"""

from __future__ import annotations

import json
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
    import os

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
