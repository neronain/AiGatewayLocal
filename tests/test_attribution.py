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
