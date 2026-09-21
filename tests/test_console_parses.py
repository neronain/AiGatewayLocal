"""สคริปต์ของคอนโซลต้อง parse ผ่าน — ไม่งั้นทั้งหน้าตาย ไม่ใช่แค่ฟีเจอร์เดียวเสีย

เคสจริง 2026-09-22: เพิ่มโค้ดจัดกลุ่มโมเดลลง `app.js` โดยตั้งชื่อตัวแปร `groups` ซึ่ง
**ถูกใช้ไปแล้วในฟังก์ชันเดียวกัน** (ผลของ `/admin/access-groups`) · `SyntaxError:
Identifier 'groups' has already been declared` ทำให้ทั้งไฟล์ไม่ถูก parse — คอนโซลขึ้นว่า
"not connected" ทุกแผงว่างเปล่า และเมนูหายไปครึ่งหนึ่ง

**ชุดเทสทั้งหมดของ repo นี้ผ่านหมดตอนนั้น** เพราะไม่มีเทสไหนเอาไฟล์ JS ไป parse เลย —
มีแต่ตัวที่ใช้ regex สแกนข้อความ ซึ่งมองข้อผิดพลาดแบบนี้ไม่เห็น

ไฟล์นี้เลยทำสิ่งเดียวคือ **ส่งทุกไฟล์ JS เข้า `node --check`** · ราคาถูกมากเทียบกับ
อาการที่มันกัน — คอนโซลตายทั้งหน้าบนเครื่องลูกค้า
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SCRIPTS = sorted(STATIC.rglob("*.js"))


def test_there_is_at_least_one_script_to_check():
    """กันเคสที่พาธเปลี่ยนแล้วเทสนี้กลายเป็นของว่างที่ผ่านตลอด"""
    assert SCRIPTS, f"ไม่เจอไฟล์ .js ใต้ {STATIC}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(STATIC)))
def test_every_console_script_parses(script: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("ไม่มี node บนเครื่องนี้ — CI มีให้")
    done = subprocess.run([node, "--check", str(script)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"{script.name} parse ไม่ผ่าน:\n{done.stderr}"
