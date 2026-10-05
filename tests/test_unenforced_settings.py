"""ค่าที่ตั้งได้ต้องมีผล — หรือไม่ก็ต้องมีคนบอกว่ามันไม่มี

`gateway.yaml` ที่แจกไปกับทุกการติดตั้งมีบล็อก `rate_limit_defaults` (60 คำขอ/นาที ·
4 คำขอพร้อมกันต่อคน) · ตรวจ 2026-10-05: ไม่มีโค้ดบรรทัดไหนอ่านมันเลย และไม่เคยมี —
แอดมินที่อ่านไฟล์เชื่อว่าเกตเวย์กันผู้ใช้คนเดียวไม่ให้ยึดทุกช่อง ทั้งที่ไม่มีอะไรกัน

ตัดสินว่า **เลิกประกาศ** ไม่ใช่เริ่มบังคับ (เหตุผลอยู่ที่ schema.RateLimitDefaults) ·
ไฟล์เก่าที่ยังมีบล็อกนี้ต้องโหลดได้ครบทุกค่า และต้องถูกบอกว่าบล็อกนั้นไม่ได้ทำอะไร
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from app.registry.store import load_snapshot

ROOT = Path(__file__).resolve().parent.parent

OLD_BLOCK = "\nrate_limit_defaults:\n  requests_per_minute: 60\n  concurrent_requests: 4\n"


def _copy_of_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


def test_a_gateway_file_that_still_has_the_block_loads_in_full(tmp_path, caplog):
    """ลบฟิลด์ออกจาก schema ตรง ๆ = ไฟล์เก่าโหลดไม่ผ่านทั้งไฟล์ แล้วค่าอื่นกลับเป็นค่าตั้งต้น"""
    config = _copy_of_config(tmp_path)
    path = config / "gateway.yaml"
    text = path.read_text(encoding="utf-8").replace("max_requests: 500", "max_requests: 7")
    path.write_text(text + OLD_BLOCK, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.registry.store"):
        snapshot = load_snapshot(config)

    assert not [e for e in snapshot.errors if "gateway.yaml" in e], snapshot.errors
    assert snapshot.gateway.quota_defaults.max_requests == 7, "ค่าอื่นในไฟล์ต้องยังถูกใช้"
    said = [r.getMessage() for r in caplog.records if "rate_limit_defaults" in r.getMessage()]
    assert len(said) == 1, said
    assert "not enforced" in said[0]
    assert "max_requests_per_minute" in said[0] and "max_concurrency" in said[0], (
        "ต้องบอกว่าของที่บังคับจริงอยู่ตรงไหน")


def test_the_shipped_gateway_file_no_longer_declares_it(caplog):
    """เตือนตอนโหลดอย่างเดียวไม่พอ ถ้าไฟล์ที่เราแจกเองยังใส่บล็อกนั้นให้ทุกการติดตั้งใหม่"""
    with caplog.at_level(logging.WARNING, logger="app.registry.store"):
        snapshot = load_snapshot(ROOT / "config")
    assert "rate_limit_defaults" not in snapshot.gateway.model_fields_set
    assert not [r for r in caplog.records if "rate_limit_defaults" in r.getMessage()]


def test_documentation_cited_from_the_code_exists():
    """`core/tokens.py` เคยชี้ไปที่ `docs/OPERATIONS.md` กับคำสั่ง `lg-measure-tokens` —
    ไม่มีทั้งคู่ · คนที่ทำตามคอมเมนต์เพื่อวัดอัตรา tokenizer ไปต่อไม่ได้ตั้งแต่ก้าวแรก
    """
    cited: dict[str, list[str]] = {}
    sources = [*ROOT.glob("app/**/*.py"), *ROOT.glob("scripts/*"), *ROOT.glob("config/**/*.yaml")]
    for source in sources:
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        for found in re.findall(r"docs/[A-Za-z0-9_.\-/]+\.(?:md|yaml)", text):
            cited.setdefault(found, []).append(str(source.relative_to(ROOT)))
    assert cited, "ตัวค้นต้องเจออะไรบ้าง ไม่งั้นเทสนี้ผ่านเพราะไม่ได้ดูอะไรเลย"
    missing = {doc: files for doc, files in cited.items() if not (ROOT / doc).exists()}
    assert not missing, missing


def test_the_tokenizer_rate_comment_points_at_a_section_that_is_there():
    """ชี้ไปไฟล์ที่มีอยู่แต่ไม่มีหัวข้อนั้นก็คือชี้ไปที่ว่างเหมือนกัน"""
    comment = (ROOT / "app" / "core" / "tokens.py").read_text(encoding="utf-8")
    section = re.search(r"docs/DEPLOYMENT\.md §(\d+\.\d+[a-z]?)", comment)
    assert section, "คอมเมนต์ควรบอกหัวข้อ ไม่ใช่แค่ชื่อไฟล์ 1,000 บรรทัด"
    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    heading = re.search(rf"^### {re.escape(section.group(1))} .*$", guide, re.M)
    assert heading, f"ไม่มีหัวข้อ {section.group(1)} ใน docs/DEPLOYMENT.md"
    body = guide[heading.end():].split("\n### ", 1)[0]
    assert "/tokenize" in body and "wide_chars_per_token" in body
