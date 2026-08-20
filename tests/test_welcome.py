"""หน้าแรกของระบบ — คนเปิดที่อยู่เกตเวย์ในเบราว์เซอร์ต้องรู้ว่าจะไปต่อทางไหน

เดิม `/` คืน JSON ให้ทุกคน · คนที่เปิดครั้งแรกจึงเจอก้อน JSON ที่ไม่บอกอะไรเลย
แต่ script และ monitoring ที่ยิงมาที่ `/` อยู่แล้วต้องไม่พัง — จึงแยกด้วย Accept
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_a_browser_gets_a_page(client):
    r = client.get("/", headers={"accept": "text/html,application/xhtml+xml,*/*"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "LiteGate" in r.text
    # ต้องมีทางไปต่อทั้งสองทาง ไม่ใช่แค่สวย
    assert 'href="/console/"' in r.text
    assert 'href="/console/member/"' in r.text


def test_a_script_still_gets_the_json_it_had(client):
    """curl ส่ง Accept: */* — ของเดิมต้องไม่พัง"""
    for accept in ("application/json", "*/*"):
        r = client.get("/", headers={"accept": accept})
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "LiteGate"
        assert body["console"] == "/console"


def test_the_page_pulls_nothing_from_the_internet():
    """ไซต์ปลายทางหลายแห่งอยู่หลัง proxy — หน้าแรกที่โหลดฟอนต์จากเน็ตจะพังตรงนั้น"""
    import re

    page = (ROOT / "app" / "static" / "welcome.html").read_text(encoding="utf-8")
    loaders = (
        re.findall(r"""<(?:script|img|iframe)[^>]*\ssrc=["']([^"']+)""", page, re.I)
        + re.findall(r"""<link[^>]*\shref=["']([^"']+)""", page, re.I)
        + re.findall(r"""@import\s+["']([^"']+)""", page, re.I)
    )
    for target in loaders:
        assert not target.lower().startswith(("http://", "https://", "//")), target


def test_the_page_says_who_made_it():
    page = (ROOT / "app" / "static" / "welcome.html").read_text(encoding="utf-8")
    assert "neronain" in page
    assert "MIT" in page


def test_health_reports_the_version_the_page_shows(client):
    assert client.get("/healthz").json()["version"]
