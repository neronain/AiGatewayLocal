"""สคริปต์อัปเดตต้องรอดบนเครื่องจริง ไม่ใช่แค่บนเครื่องที่มีของครบ

ทุกข้อในไฟล์นี้มาจากการกดปุ่มจริงบนเกตเวย์แล้วล้ม (2026-09-22) — สี่รอบกว่าจะผ่าน:

1. `GW_UPDATE_SOURCE` อยู่ใน `.env` ซึ่ง **unit ของ updater ไม่ได้โหลด** (มีแต่
   `litegate.service` ที่มี `EnvironmentFile`) → อ่านจากไฟล์ตรง ๆ ด้วย
2. **ไม่มี `git`** บนเกตเวย์ → `git pull` เป็นของอำนวยความสะดวก ไม่ใช่เงื่อนไขบังคับ
3. เทียบ `pyproject.toml` ทั้งไฟล์ → ดังทุกครั้งที่เลขเวอร์ชันขยับ ซึ่งคือทุกรุ่น
4. **ไม่มี `rsync`** บนเกตเวย์ → ต้องมีทางสำรองที่ยังลบไฟล์ที่หายไปในรุ่นใหม่ได้จริง

เทสที่นี่อ่านตัวสคริปต์ ไม่ได้รันมัน — มันรันเป็น root และรีสตาร์ต service จริง
สิ่งที่ตรวจได้จึงเป็น **คุณสมบัติที่ต้องไม่หายไป** ไม่ใช่พฤติกรรมตอนรัน
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "self_update.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
CODE = "\n".join(x for x in TEXT.splitlines() if not x.strip().startswith("#"))


def test_the_script_parses():
    if not shutil.which("bash"):
        pytest.skip("ไม่มี bash")
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_it_is_executable():
    """path unit เรียกไฟล์นี้ตรง ๆ — ไม่ +x แล้วปุ่มตายเงียบ"""
    assert SCRIPT.stat().st_mode & 0o111


def test_missing_git_does_not_fail_the_whole_update():
    """งานของสคริปต์คือติดตั้งสิ่งที่อยู่ในโฟลเดอร์ต้นทาง · `git pull` เป็นของแถม
    โฟลเดอร์นั้นอาจถูก rsync มา อาจ pull ด้วยมือแล้ว หรือเครื่องอาจไม่มี git เลย"""
    assert "command -v git" in CODE, "ต้องเช็คก่อนใช้"
    # ต้องไม่มี `git pull` ที่ล้มแล้วพาทั้งสคริปต์ล้มตาม
    for line in CODE.splitlines():
        if "git -C" in line and "pull" in line:
            assert line.strip().startswith("elif") or "||" in line or "if " in line, line


def test_missing_rsync_still_removes_files_deleted_in_the_new_version():
    """ไฟล์เก่าที่ค้างอยู่จะถูก import แทนของใหม่ — เป็นอาการที่หาสาเหตุยากที่สุดแบบหนึ่ง
    ทางสำรองต้องให้ผลเหมือน `rsync --delete` ไม่ใช่แค่ก๊อปทับ"""
    assert "command -v rsync" in CODE
    assert "rm -rf" in CODE and "app.incoming" in CODE, "ทางสำรองต้องสร้างโฟลเดอร์ใหม่ทั้งก้อน"
    assert "__pycache__" in CODE, "ทั้งสองทางต้องไม่ลาก __pycache__ ของเครื่องต้นทางมาด้วย"


def test_the_dependency_gate_ignores_the_version_line():
    """เลขเวอร์ชันอยู่ใน pyproject และขยับทุกรุ่น — เทียบทั้งไฟล์จะบล็อกทุกการอัปเดต"""
    assert "tomllib" in TEXT, "ต้อง parse แล้วเทียบเฉพาะส่วน dependency"
    assert "optional-dependencies" in TEXT and "requires-python" in TEXT
    assert 'cmp -s "${SOURCE}/pyproject.toml"' not in CODE, "เลิกเทียบทั้งไฟล์แล้ว"


def test_the_installed_pyproject_is_refreshed_so_the_gate_can_clear():
    """`pip install -e` ติดตั้ง dependency แต่ไม่ได้ก๊อป pyproject มาให้ — ไม่อัปเดต
    บันทึกนี้แปลว่าด่านจะดังซ้ำตลอดไปแม้ลง dependency ไปแล้ว (ทางตันจริงที่เจอมา)"""
    assert 'install -o "$OWNER"' in CODE and "pyproject.toml" in CODE


def test_an_operator_can_confirm_the_dependencies_are_in_place():
    """เครื่องตรวจแทนไม่ได้ว่า venv ตรงกับรุ่นใหม่แล้ว — ต้องถามคนที่เพิ่งลงเอง"""
    assert "SKIP_DEPS" in CODE
    assert "update.force" in TEXT


def test_a_failure_after_writing_starts_rolls_back():
    """ล้มก่อนแตะไฟล์ = ไม่ต้องทำอะไร · ล้มหลังจากนั้น = ต้องกู้คืน ไม่ใช่ทิ้งไว้ครึ่ง ๆ"""
    assert "WRITING=1" in CODE
    assert "if [ \"$WRITING\" = 1 ]; then restore" in CODE


def test_the_request_and_force_flags_never_outlive_one_run():
    """ไฟล์คำขอที่ค้าง = path unit ยิงซ้ำไม่รู้จบ · flag ที่ค้าง = ครั้งหน้าข้ามด่านเอง"""
    assert 'rm -f "$REQUEST" "$FORCE"' in CODE


def test_it_never_touches_anything_but_the_app_directory():
    """`.env` มีความลับ · `config/` เป็นของผู้ดูแล · `data/` คือฐานข้อมูล"""
    for danger in ("/.env", "/config/", "/data/gateway.db", ".venv/bin/pip install"):
        for line in CODE.splitlines():
            if danger in line:
                # อ่านได้ แต่ห้ามเขียนทับ
                writes = ("rm -rf", "rsync", "mv ", "install -o", "> \"")
                assert not any(w in line for w in writes), line


# ── เก็บกวาด backup ───────────────────────────────────────────────────────────
#
# เจอจริงบนเครื่องเดโม: สคริปต์สำรอง app/ ทุกครั้งที่กด Update แต่ไม่เคยลบของเก่า
# สะสมไป 19 ชุด 474MB เครื่องลูกค้าที่กดทุกสัปดาห์จะเต็ม /opt ในปีเดียว
def test_prune_runs_only_after_a_healthy_gateway():
    """ล้มกลางทางต้องเหลือ backup ครบ — ไม่งั้นไล่ย้อนไม่ได้ตอนที่ต้องการที่สุด"""
    body = SCRIPT.read_text()
    after_ok = body.split('say "เกตเวย์กลับมาแล้ว')[1]
    assert "prune_backups" in after_ok.split("else")[0]
    # ต้องไม่ถูกเรียกในเส้นทางที่ rollback
    for branch in ("import ไม่ผ่าน", "ไม่ตอบหลัง restart"):
        seg = body.split(branch)[1][:200]
        assert "prune_backups" not in seg


def test_prune_keeps_five_and_deletes_the_rest(tmp_path):
    parent = tmp_path / "opt"
    parent.mkdir()
    install = parent / "litegate"
    (install / "app").mkdir(parents=True)
    made = []
    for day in range(1, 9):
        d = parent / f"gw-backup-2026090{day}-120000"
        d.mkdir()
        made.append(d)
    # ของที่คนอื่นวางไว้ ชื่อไม่ตรงแบบแผน — ห้ามแตะ
    keep_foreign = [parent / "gw-backup-static-235542", parent / "gw-backup-unit-x.service"]
    for d in keep_foreign:
        d.mkdir()

    _run_prune(install, made[-1])

    left = sorted(p.name for p in parent.glob("gw-backup-*"))
    assert "gw-backup-static-235542" in left
    assert "gw-backup-unit-x.service" in left
    dated = [n for n in left if n.startswith("gw-backup-2026")]
    assert len(dated) == 5, dated
    assert dated == sorted(dated)[-5:]          # เหลือห้าชุดที่ใหม่ที่สุด


def test_prune_never_deletes_this_runs_backup(tmp_path):
    """ถ้า BACKUP ของรอบนี้บังเอิญหลุดเข้าลิสต์ ก็ยังต้องรอด"""
    parent = tmp_path / "opt"
    parent.mkdir()
    install = parent / "litegate"
    (install / "app").mkdir(parents=True)
    for day in range(1, 9):
        (parent / f"gw-backup-2026090{day}-120000").mkdir()
    mine = parent / "gw-backup-20260901-120000"     # ตัวที่เก่าที่สุด = โดนลบแน่ถ้าไม่กัน

    _run_prune(install, mine)

    assert mine.exists()


def _run_prune(install_dir, backup):
    """ดึงฟังก์ชัน prune_backups ออกมารันจริงด้วย bash — ไม่ใช่อ่านข้อความเอา"""
    import subprocess
    body = SCRIPT.read_text()
    start = body.index("KEEP_BACKUPS=")
    end = body.index("restore() {")
    snippet = body[start:end]
    script = (
        f'INSTALL_DIR="{install_dir}"\n'
        f'BACKUP="{backup}"\n'
        'say() { :; }\n'
        f"{snippet}\n"
        "prune_backups\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
