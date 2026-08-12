"""Find UI labels still written in Thai.

Both consoles get handed to universities and companies who may not read Thai,
so the chrome has to be one language. Doing that by eye does not work: the
first pass through this codebase caught the buttons and the section headings
and left twenty labels behind in dropdown options, form labels and
placeholders - exactly the sort of thing that reads as unfinished.

The line this draws:

  * **Text inside a control** - button, heading, form label, dropdown option,
    table header, placeholder, expander summary - must be English. People scan
    these without meaning to read them, and a second language there interrupts
    every time.
  * **Text that explains** - tooltips, and the paragraphs under a heading - may
    stay Thai. That is what someone reads when they are genuinely unsure, and
    their own language helps most exactly then.

Usage:

    python scripts/audit_ui_language.py app/static/index.html app/static/app.js

Prints every label still needing translation, grouped by the kind of control it
sits in. Tooltips are reported too, so keeping them Thai stays a decision rather
than an oversight.
"""

import html
import re
import sys
from pathlib import Path

THAI = re.compile(r"[฀-๿]")

# (ชื่อชนิด, regex ที่ดึงข้อความออกมา)
LABEL_PATTERNS = [
    ("button",      re.compile(r"<button[^>]*>(.*?)</button>", re.S)),
    ("heading",     re.compile(r"<h[1-4][^>]*>(.*?)</h[1-4]>", re.S)),
    ("label",       re.compile(r"<label[^>]*>(.*?)</label>", re.S)),
    ("option",      re.compile(r"<option[^>]*>(.*?)</option>", re.S)),
    ("th",          re.compile(r"<th[^>]*>(.*?)</th>", re.S)),
    ("section",     re.compile(r'class="sec"[^>]*>(.*?)</div>', re.S)),
    ("summary",     re.compile(r"<summary[^>]*>(.*?)</summary>", re.S)),
    ("placeholder", re.compile(r'placeholder="([^"]*)"')),
    ("title-attr",  re.compile(r'title="([^"]*)"')),
]


def scan(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    found: list[tuple[str, str]] = []
    for kind, pattern in LABEL_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1)
            # ข้ามส่วนที่เป็น markup ซ้อน (เช่นปุ่มที่มี <svg> ข้างใน) เอาเฉพาะข้อความ
            plain = html.unescape(re.sub(r"<[^>]+>", " ", raw)).strip()
            plain = re.sub(r"\$\{[^}]*\}", "", plain).strip()   # template literal
            if plain and THAI.search(plain):
                found.append((kind, " ".join(plain.split())[:90]))
    return found


for name in sys.argv[1:]:
    path = Path(name)
    results = scan(path)
    print(f"\n===== {path} — {len(results)} label(s) still Thai =====")
    seen = set()
    for kind, value in results:
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        print(f"  [{kind:11}] {value}")
