"""ป้ายเป็นอังกฤษ คำอธิบายเป็นไทย — กติกาที่ผู้ใช้ตั้งไว้

ป้ายคือสิ่งที่คนกวาดตาหาเพื่อจะกด · คำอธิบายคือสิ่งที่อ่านตอนไม่แน่ใจ สองอย่างนี้
ต้องการภาษาคนละแบบ และสลับกันไม่ได้
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app" / "static"
THAI = re.compile(r"[฀-๿]")


def _strip(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return " ".join(re.sub(r"\$\{[^}]*\}", "", text).split())


@pytest.mark.parametrize("tag", ["label", "button", "h1", "h2", "h3", "th", "option"])
def test_no_thai_in_the_markup_labels(tag: str):
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.S):
        text = _strip(m.group(1))
        if THAI.search(text):
            bad.append(f"บรรทัด {html[:m.start()].count(chr(10)) + 1}: {text[:60]}")
    assert bad == [], f"<{tag}> ที่เป็นภาษาไทย:\n" + "\n".join(bad)


def test_no_thai_in_labels_the_script_builds():
    """ปุ่มที่ JS สร้างก็เป็นป้ายเหมือนกัน — ตรวจแค่ HTML จึงไม่พอ

    ต้องอ่านข้ามบรรทัดด้วย · เวอร์ชันแรกของเทสต์นี้ดูทีละบรรทัดแล้วพลาดปุ่มที่ขึ้น
    บรรทัดใหม่ระหว่าง <button ...> กับข้อความ ซึ่งคือรูปแบบที่ใช้มากที่สุดในไฟล์นี้
    """
    js = (ROOT / "app.js").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    body = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))

    bad = []
    for m in re.finditer(r"<(button|label|th|h3)\b.*?>(.*?)</\1>", body, re.S):
        text = _strip(m.group(2))
        if THAI.search(text):
            bad.append(f"บรรทัด {body[:m.start()].count(chr(10)) + 1}: <{m.group(1)}> {text[:50]}")
    assert bad == [], "ป้ายที่ JS สร้างเป็นภาษาไทย:\n" + "\n".join(bad)


def test_the_thai_explanations_are_still_there():
    """กติกาคือ *ป้าย* เป็นอังกฤษ ไม่ใช่ทั้งหน้าเป็นอังกฤษ

    ถ้าใครไล่แปลจนคำอธิบายหายไปด้วย ผู้ใช้จะเสียสิ่งที่ช่วยได้จริงตอนไม่แน่ใจ
    """
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    hints = [_strip(m.group(1)) for m in re.finditer(r'class="hint[^"]*"[^>]*>(.*?)</', html, re.S)]
    assert sum(1 for h in hints if THAI.search(h)) >= 10
