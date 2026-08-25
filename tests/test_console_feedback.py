"""คอนโซลตอบกลับตรงที่ผู้ใช้กด ไม่ใช่ตรงที่โครงหน้าเว็บวางไว้

ลูกค้าแจ้งว่า "หน้า assistant กด Deploy tool แล้วไม่มีอะไรเกิดขึ้น" · ปุ่มทำงาน
ปกติ คำตอบก็มา แต่คำตอบทุกอย่างของคอนโซล — ทั้ง error และข้อความสำเร็จ — ไปลง
กล่องเดียวที่บนสุดของ <main> ซึ่งบนแท็บ assistant อยู่สูงกว่าปุ่มราวสองพันเจ็ดร้อย
พิกเซล กดแล้วจึงไม่มีอะไรเปลี่ยนในส่วนของหน้าที่คนกำลังมองอยู่จริงๆ
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
ROOT_APP = Path(__file__).resolve().parents[1] / "app"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_the_answer_follows_the_reader_down_the_page():
    block = CSS.split("#error {", 1)[1].split("}", 1)[0]
    assert "position: sticky" in block, "กล่องคำตอบต้องเกาะจอ ไม่ใช่ค้างอยู่หัวหน้า"
    assert "z-index" in block, "ต้องอยู่เหนือเนื้อหา ไม่งั้นโดนการ์ดทับ"


def test_it_parks_under_whatever_height_the_header_actually_is():
    """จอแคบแถวเมนูตัดบรรทัดแล้ว header สูงขึ้น — ตรึงตัวเลขตายตัวไว้จะไปบังเมนู"""
    body = JS.split("function banner(", 1)[1].split("\n}", 1)[0]
    assert "offsetHeight" in body
    assert "querySelector('header')" in body


def test_a_message_can_be_put_away():
    """แถบนี้ลอยอยู่เหนือเนื้อหา ค้างไว้ก็บังของข้างหลัง"""
    body = JS.split("function banner(", 1)[1].split("\n}", 1)[0]
    assert "onclick" in body and "innerHTML = ''" in body


def test_a_confirmation_leaves_but_a_failure_stays():
    """คนที่พลาดมักต้องอ่านซ้ำ · คนที่สำเร็จอ่านครั้งเดียวก็พอ"""
    body = JS.split("function banner(", 1)[1].split("\n}", 1)[0]
    assert "kind === 'ok'" in body and "setTimeout" in body


def test_a_block_before_the_gateway_says_so_instead_of_request_failed():
    """403 ที่ไม่ใช่ JSON ไม่ได้มาจากเกตเวย์ — nginx ตอบแทนตาม allow list

    คอนโซลเรียก /admin/ เกือบทุกปุ่ม พอโดนบล็อกจึงพังทั้งหน้าพร้อมกัน และสิ่งที่
    ขึ้นมาคือ "403: request failed" ซึ่งไม่ได้ชี้ไปที่ไฟล์ไหนหรือแก้อย่างไรเลย
    """
    body = JS.split("async function api(", 1)[1].split("\nconst post", 1)[0]
    assert "JSON.parse" in body
    assert "response.status === 403" in body
    assert "allow list" in body
    assert "litegate.conf" in body, "ต้องบอกไฟล์ที่ต้องไปแก้"


def test_a_member_can_get_their_own_client_config_without_asking_anyone():
    """คนถือคีย์ต้องแปลงเอกสารเป็นค่าของตัวเองทุกครั้ง — base URL, alias, คีย์

    สามอย่างนั้นคอนโซลรู้อยู่แล้ว · ปล่อยให้เดาเองคือให้ผู้ดูแลตอบคำถามเดิม
    ทีละคน ซึ่งเป็นสิ่งที่เสียเวลาที่สุดตอนเปิดใช้กับห้องเรียนใหม่
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for el in ("ct-tool", "ct-model", "ct-key", "ct-out", "ct-copy"):
        assert f'id="{el}"' in html, f"ขาดช่อง {el}"
    # แท็บนี้เคยเห็นได้เฉพาะ staff เพราะมีแต่ของโหลด — สมาชิกต้องเข้าถึงส่วนตั้งค่าได้
    tab = html.split('data-tab="tools"', 1)[1].split(">", 1)[0]
    assert "data-staff" not in tab and "data-admin" not in tab


def test_the_claude_code_config_sets_every_tier_it_actually_asks_for():
    """Claude Code เรียกรุ่นเล็กทำงานเบื้องหลังเอง คนละตัวกับที่ผู้ใช้เลือก

    ตั้งแต่ ANTHROPIC_MODEL อย่างเดียวจะพังกลางทางตอนมันไปขอรุ่นที่เกตเวย์ไม่รู้จัก
    ซึ่งโผล่เป็น error ที่ไม่เกี่ยวกับสิ่งที่ผู้ใช้เพิ่งทำ
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    block = js.split("'claude-code': {", 1)[1].split("\n  kilo: {", 1)[0]
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL"):
        assert var in block, f"ขาด {var}"
    # กับดักที่พิสูจน์มาแล้ว: เครื่องที่ล็อกอินบัญชีอยู่จะส่งโทเคน OAuth แทนคีย์นี้
    assert "logout" in block


def test_the_model_list_is_deduplicated_and_matches_the_protocol():
    """แค็ตตาล็อกจัดกลุ่มตาม purpose · รุ่นเดียวอยู่ได้หลายกลุ่ม

    ไม่ตัดซ้ำแล้วรายการจะขึ้นชื่อเดิมสามสี่รอบ ซึ่งอ่านแล้วเหมือนระบบนับผิด
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js.split("function ctModels(", 1)[1].split("\n}", 1)[0]
    assert "seen" in body and "Map()" in body
    assert "protocols" in body, "ต้องกรองตามโปรโตคอลที่เครื่องมือนั้นพูด"
    assert "supports_tools" in body, "เอเจนต์ที่เรียกเครื่องมือไม่ได้ พังตอนใช้จริงไม่ใช่ตอนตั้งค่า"


def test_editing_a_model_in_the_console_keeps_routing_rules_it_cannot_show():
    """คอนโซลประกอบ spec ใหม่ทุกครั้งที่กด Save

    ฟิลด์ไหนที่หน้าจอไม่ได้ส่งกลับไปด้วยจะหายทันที · `overflow` กับ
    `small_prompt` ยังไม่มีช่องบนหน้าจอ การแก้ชื่อรุ่นครั้งเดียวจึงลบกฎที่คนอื่น
    ตั้งไว้ทิ้งได้ โดยไม่มีอะไรบอกว่าเกิดขึ้น
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js.split("const routing = {", 1)[1].split("definition.spec.routing", 1)[0]
    assert "editingRouting" in js, "ต้องเก็บ routing เดิมไว้ตอนเปิดแก้"
    assert "readFallback()" in body
    # ส่งกลับเฉพาะที่มีค่า — ไม่งั้น overflow: null จะถูกเขียนลง YAML ทุกครั้ง
    assert "filter" in body


def test_the_admin_model_list_returns_routing_so_the_form_can_round_trip():
    admin = (ROOT_APP / "api" / "admin.py").read_text(encoding="utf-8")
    block = admin.split('@router.get("/models")', 1)[1].split("@router.", 1)[0]
    assert '"routing": model.spec.routing.model_dump()' in block


def test_a_quota_window_says_how_long_is_left_not_just_when_it_ends():
    """คนที่โดนตัดโควตาอยากรู้อย่างเดียวว่าต้องรออีกนานแค่ไหน

    เวลาสัมบูรณ์บังคับให้คิดเลขในหัวก่อน · เก็บไว้ในวงเล็บสำหรับคนที่ต้องจดต่อ
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function untilText(" in js
    assert "data-until" in js
    # ตัวจับเวลาต้องมีตัวเดียว ไม่งั้นเปิดแท็บกลับไปกลับมาแล้ว interval สะสม
    assert "if (countdownTimer) return;" in js


def test_the_menu_is_a_sidebar_not_a_row_that_wraps():
    """แถบแนวนอนตัดบรรทัดบนจอ 375px จน header สูง 230px

    กินจอไปหนึ่งในสี่ก่อนเห็นเนื้อหาแถวแรก · sidebar ลดเหลือ 136px
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert 'id="shell"' in html and 'id="side"' in html
    # nav ต้องออกจาก header แล้ว ไม่งั้นยังกินความสูงเหมือนเดิม
    assert "<nav" not in html.split("</header>", 1)[0]
    assert "grid-template-columns: 196px" in css


def test_the_drawer_can_be_closed_three_ways():
    """คนกดเมนูเพื่อไปที่อื่น ไม่ใช่เพื่อค้างเมนูไว้ดู"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "setNav(false)" in js.split("btn.onclick", 1)[1][:120], "เลือกแท็บแล้วต้องปิดเอง"
    assert "navscrim" in js, "กดพื้นหลังต้องปิด"
    assert "'Escape'" in js, "Esc ต้องปิด"


def test_a_group_heading_never_outlives_the_group():
    """สมาชิกไม่มีสิทธิ์เห็นเมนูใต้ People/System

    ซ่อนแต่ปุ่มจะเหลือหัวข้อลอยอยู่โดยไม่มีอะไรอยู่ข้างใต้ ซึ่งอ่านแล้วเหมือน
    ระบบโหลดไม่ครบ
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '<p class="navgroup" data-staff>' in html
    assert '<p class="navgroup" data-admin>' in html
    # ตัวกรองต้องกวาดลูกทุกตัวของ #tabs ไม่ใช่เฉพาะ button
    assert "querySelectorAll('#tabs > *')" in js
