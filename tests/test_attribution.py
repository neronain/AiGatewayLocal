"""ลายเซ็นต้องอยู่ในที่ที่ผู้ใช้ปลายทางเห็น ไม่ใช่แค่ในไฟล์ที่ลบทิ้งได้บรรทัดเดียว

เคสจริง 2026-08-20: มีคนเอา repo ไปพัฒนาต่อโดยไม่ให้เครดิต · LiteGate เป็น MIT ซึ่ง
*อนุญาตให้ fork* แต่ **บังคับให้คงประกาศลิขสิทธิ์ไว้** — การถอดออกคือผิดสัญญาอนุญาต
"""

from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parents[1]


def test_attribution_has_one_source_of_truth():
    """ข้อความที่ก๊อปกระจายหลายไฟล์จะแก้ไม่ทั่ว แล้วบางจุดหายไปเงียบ ๆ"""
    assert config.AUTHOR == "neronain"
    assert "neronain.minidev" in config.AUTHOR_URL
    assert config.PRODUCT == "LiteGate"
    assert "MIT" in config.LICENSE_NOTE


def test_every_response_carries_the_signature():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'response.headers["x-litegate-by"] = config.AUTHOR' in main, (
        "header ต้องตั้งใน middleware ที่เดียว ไม่ใช่ไล่แก้ทุก endpoint"
    )


def test_healthz_reports_who_built_it():
    health = (ROOT / "app" / "api" / "health.py").read_text(encoding="utf-8")
    for field in ('"product"', '"built_by"', '"author_url"', '"license"'):
        assert field in health, field


def test_console_shows_a_credit_footer():
    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="lg-credit"' in page
    assert "neronain" in page
    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert "#lg-credit" in css, "footer ที่ไม่มีสไตล์ = footer ที่ไม่มีใครเห็น"


def test_license_and_notice_ship_with_the_code():
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in licence and "neronain" in licence
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "attribution" in notice.lower()
    # NOTICE ต้องบอกด้วยว่า LMDS เป็นคนละสัญญาอนุญาต ไม่ให้เข้าใจผิดว่า fork ได้เหมือนกัน
    assert "proprietary" in notice.lower() or "NOT covered" in notice


def test_catalog_sections_can_be_folded():
    """โมเดลตัวเดียวโผล่ได้หลายหมวด — พอ registry โต หน้าจะยาวหลายจอโดยเนื้อหาซ้ำ"""
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "CATALOG_FOLD_KEY" in js, "สถานะยุบ/กางต้องถูกจำไว้"
    assert "<details class=\"model-section-box\"" in js
    # ยังไม่เคยเลือกเอง = กางหมวดแรก · เลือกแล้วต้องเคารพ รวมถึงกรณี "ยุบหมด"
    assert "remembered === null ? index === 0" in js
    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".model-section-box" in css


def test_the_version_has_one_source():
    """เดิมฝังเลขเวอร์ชันตายสองที่ใน main.py แล้วลืมอัปตอน bump — `/` ประกาศเลขเก่าค้าง"""
    import tomllib

    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '"1.' not in main.split("def create_app")[1][:2000], "อย่าฝังเลขเวอร์ชันใน main.py"
    assert "config.VERSION" in main

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config.VERSION == declared["project"]["version"], (
        "config.VERSION กับ pyproject ต้องตรงกัน ไม่งั้นเลขที่ประกาศออกไปเชื่อไม่ได้"
    )


def test_long_sections_can_be_folded():
    """แท็บ Access & keys มีสี่ส่วนยาว ๆ ต่อกัน — พอผู้ใช้จริงมีคนเป็นสิบและ key เป็นสิบใบ
    ส่วนที่อยากดูจะอยู่ล่างสุดเสมอ
    """
    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for key in ("people", "apikeys", "groups", "wsmodels"):
        assert f'data-fold-section="{key}"' in page, key

    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function setupFoldSections" in js
    assert "SECTION_FOLD_KEY" in js, "สถานะพับต้องถูกจำไว้ ไม่ใช่กางกลับทุกครั้ง"
    # เป็นกลไกกลาง หมวดใหม่ต้องได้ฟรีด้วยการใส่ attribute เดียว
    assert "querySelectorAll('[data-fold-section]')" in js
    assert "setupFoldSections();" in js, "ต้องถูกเรียกตอน boot"

    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".fold-toggle" in css


def test_every_long_tab_has_foldable_sections():
    """ไม่ใช่แค่แท็บเดียว — ทุกแท็บที่มีหลายหมวดควรพับได้เหมือนกัน"""
    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for key in ("acct-keys", "dash-models", "reg-models", "asst-console",
                "quota-policies", "client-tools"):
        assert f'data-fold-section="{key}"' in page, key


def test_a_marker_never_sits_on_a_header_row():
    """เกาะ .bar จะพับปุ่มแทนเนื้อหา — เจอจริงตอนทำ acct-keys กับ reg-models"""
    import re

    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for line in page.splitlines():
        if "data-fold-section" not in line:
            continue
        assert not re.search(r'data-fold-section="[^"]+"\s+class="(bar|field|row)\b', line), line.strip()


def test_a_whole_tab_can_be_folded_at_once():
    """พับทีละหมวดยังช้าเมื่อแท็บหนึ่งมีสี่ห้าหมวด"""
    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="fold-all"' in page and 'id="unfold-all"' in page

    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function foldAllInTab" in js
    # ต้องทำเฉพาะแท็บที่เห็นอยู่ — ไม่ล้างสถานะที่ผู้ใช้ตั้งใจไว้ในแท็บอื่น
    assert 'section[id^="tab-"]:not([hidden])' in js
