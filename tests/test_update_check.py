"""ปุ่ม "มีรุ่นใหม่ไหม" — ตรวจเมื่อคนกด และส่งออกไปเปล่า ๆ

เครื่อง production เคยค้างที่ 1.10.0 หลายสัปดาห์หลัง 1.12.1 ออกไปแล้ว เพราะไม่มีอะไร
บนจอบอก · สิ่งที่แก้ปัญหานั้นคือปุ่มเดียว แต่สิ่งที่ทำให้ปุ่มนั้นยอมรับได้คือข้อจำกัด
รอบตัวมัน และข้อจำกัดพวกนั้นคือสิ่งที่ไฟล์นี้เฝ้าไว้:

* เกตเวย์ที่บูตขึ้นมาแล้วมีคนเปิดหน้าคอนโซล ต้องไม่มีอะไรออกเน็ตเลย
* คำขอที่ออกไปต้องไม่พกเลขเวอร์ชัน ชื่อเครื่อง หรือสถิติใด ๆ ไปด้วย
* ต่อไม่ได้ต้องได้คำตอบ ไม่ใช่หน้าจอแดง
* เบราว์เซอร์ต้องไม่เป็นคนยิงเอง มิฉะนั้น IP ของคนเปิดคอนโซลจะไปโผล่ข้างนอก

ทุกเทสในไฟล์นี้ mock GitHub ไว้หมด · ไม่มีเทสไหนแตะเน็ตจริง
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import httpx
import pytest
import respx

from app import config
from app.core import release

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
LATEST = release.LATEST_RELEASE_URL
TAGS = release.TAGS_URL


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _bump(version: str, step: int = 1) -> str:
    """เลขที่ใหญ่กว่า/เล็กกว่าที่รันอยู่ — คำนวณจากของจริง จะได้ไม่ผูกกับ 1.12.1"""
    parts = list(release.parse_version(version))
    parts[0] += step
    return "v" + ".".join(str(p) for p in parts)


def _release(tag: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "tag_name": tag,
            "name": f"LiteGate {tag}",
            "html_url": f"https://github.com/{release.REPO}/releases/tag/{tag}",
            "published_at": "2026-09-01T10:00:00Z",
        },
    )


# ---------------------------------------------------------------------------
# ยิงเมื่อกดเท่านั้น
# ---------------------------------------------------------------------------
@respx.mock
def test_nothing_goes_out_while_the_gateway_boots_or_a_page_loads(temp_db):
    """ข้อนี้คือเหตุผลที่ฟีเจอร์นี้ยอมให้มีได้

    ลูกค้า air-gapped มีจริง และเป็นเหตุผลที่คอนโซลนี้ไม่โหลดแม้แต่ฟอนต์จากข้างนอก ·
    ถ้าวันหนึ่งมีใครเติม "ตรวจให้อัตโนมัติตอนเปิดหน้า" เข้ามา เทสนี้ต้องแตก
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        client.get("/")
        client.get("/healthz")
        client.get("/static/app.js")

    assert respx.calls.call_count == 0, (
        "มีคำขอออกนอกเครื่องตอนบูตหรือตอนโหลดหน้า: "
        f"{[str(c.request.url) for c in respx.calls]}"
    )


@respx.mock
def test_pressing_the_button_makes_exactly_one_request(client):
    """คู่ของเทสข้างบน — ถ้าอันนี้ไม่นับ แปลว่าอันข้างบนนับอะไรไม่ได้เลย"""
    route = respx.get(LATEST).mock(return_value=_release(_bump(config.VERSION)))

    client.post("/admin/version/check", headers=auth(client.admin_key))

    assert route.call_count == 1
    assert respx.calls.call_count == 1


@respx.mock
def test_a_caller_without_admin_rights_causes_no_request_at_all(client, member_key):
    """auth มาก่อน network · ไม่งั้นใครก็ได้ที่มีคีย์สั่งให้เครื่องนี้ต่อออกเน็ตได้"""
    denied = client.post("/admin/version/check", headers=auth(member_key))
    anonymous = client.post("/admin/version/check")

    assert denied.status_code == 403
    assert anonymous.status_code == 401
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# สิ่งที่ออกไปจริง ๆ
# ---------------------------------------------------------------------------
@respx.mock
def test_the_request_carries_nothing_about_this_machine(client):
    """SECURITY: ไม่มี telemetry · GitHub ต้องรู้แค่ว่ามีคนอ่าน release ของ repo สาธารณะ"""
    route = respx.get(LATEST).mock(return_value=_release(config.VERSION))

    client.post("/admin/version/check", headers=auth(client.admin_key))

    request = route.calls[0].request
    assert request.method == "GET"
    assert str(request.url) == LATEST
    assert not request.url.params, "ห้ามมี query string — นั่นคือที่ที่ข้อมูลรั่วออกไปง่ายที่สุด"
    assert not request.content, "ห้ามมี body"

    # header ที่ออกไปต้องเป็นของมาตรฐานล้วน ๆ · ไม่มีช่องไหนบอกว่าเครื่องนี้คือใคร
    assert set(request.headers.keys()) <= {
        "host", "accept", "accept-encoding", "connection", "user-agent",
    }, f"header แปลกปลอม: {dict(request.headers)}"

    # URL คือที่อยู่ของ repo สาธารณะ จึงมีชื่อเจ้าของ repo อยู่ตามธรรมชาติ · สิ่งที่ต้อง
    # ไม่มีคือ *อะไรก็ตามที่มาจากเครื่องนี้* และที่เดียวที่มันจะแอบไปได้คือ header
    headers = " ".join(request.headers.values()).lower()
    for leak in (config.VERSION, "litegate", config.PRODUCT, socket.gethostname()):
        assert leak.lower() not in headers, f"{leak!r} หลุดออกไปกับ header"


def test_the_module_sends_no_identifying_header_of_its_own():
    """อ่านจากค่าคงที่ตรง ๆ — เผื่อวันหนึ่งมีใครเติม User-Agent ที่ระบุตัวสินค้า"""
    assert set(release._HEADERS) == {"accept"}


# ---------------------------------------------------------------------------
# การเทียบเลข
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("1.10.0", "v1.12.1", "behind"),
        ("1.12.1", "v1.12.1", "current"),
        ("1.12.1", "1.12.1", "current"),
        ("1.12.1", "v1.12", "ahead"),
        ("1.12", "v1.12.0", "current"),
        ("1.12.1", "v1.11.9", "ahead"),
        ("1.12.1", "nightly", "unknown"),
        ("1.12.1", "", "unknown"),
    ],
)
def test_version_comparison(current: str, latest: str, expected: str):
    assert release.compare(current, latest) == expected


def test_a_tag_that_is_not_a_version_is_not_guessed_at():
    """`release-2026` ต้องไม่ถูกอ่านเป็นเวอร์ชัน 2026 แล้วบอกว่าตามหลังอยู่พันรุ่น"""
    assert release.parse_version("release-2026") is None
    assert release.parse_version("v1.12.1") == (1, 12, 1)
    assert release.parse_version("1.12.1-rc1") == (1, 12, 1)


@respx.mock
def test_a_newer_release_is_reported_with_the_commands_to_run(client):
    newer = _bump(config.VERSION)
    respx.get(LATEST).mock(return_value=_release(newer))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["ok"] is True
    assert body["status"] == "behind"
    assert body["current"] == config.VERSION
    assert body["latest"] == newer
    assert body["update"]["commands"], "บอกว่าตามหลังแล้วต้องบอกด้วยว่าต้องรันอะไร"
    assert body["update"]["shape"] in {"checkout", "container", "copied"}


@respx.mock
def test_the_current_release_is_not_reported_as_an_update(client):
    respx.get(LATEST).mock(return_value=_release(f"v{config.VERSION}"))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["status"] == "current"


@respx.mock
def test_a_build_ahead_of_the_release_is_not_called_outdated(client):
    """เครื่องที่รันจาก main ระหว่างรอ release ถัดไปมีจริง และมันไม่ได้ล้าสมัย"""
    respx.get(LATEST).mock(return_value=_release(_bump(config.VERSION, -1)))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["status"] == "ahead"


# ---------------------------------------------------------------------------
# ต่อไม่ได้ ไม่ใช่ความผิดพลาด
# ---------------------------------------------------------------------------
@respx.mock
def test_an_air_gapped_gateway_gets_an_answer_not_an_error(client):
    """ไม่มี default route คือการตั้งค่าที่ตั้งใจ ไม่ใช่อาการเสีย"""
    respx.get(LATEST).mock(side_effect=httpx.ConnectError("Network is unreachable"))

    response = client.post("/admin/version/check", headers=auth(client.admin_key))
    body = response.json()

    assert response.status_code == 200, "ตรวจไม่ได้ ต้องไม่กลายเป็น 5xx"
    assert body["ok"] is False
    assert body["status"] == "unknown"
    assert body["reason"]
    assert body["current"] == config.VERSION
    # ยังบอกวิธีอัปเดตได้อยู่ — ส่วนนั้นอ่านจากดิสก์ ไม่ได้ถามใคร
    assert body["update"]["commands"]


@respx.mock
def test_a_slow_github_does_not_hang_the_console(client):
    respx.get(LATEST).mock(side_effect=httpx.ConnectTimeout("timed out"))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["ok"] is False
    assert "in time" in body["reason"]


def test_the_timeout_is_short_enough_that_somebody_waits_for_it():
    """คนกดปุ่มแล้วยืนรออยู่ · connect คือตัวที่กำหนดว่า air-gapped จะรอกี่วินาที"""
    assert release._TIMEOUT.connect <= 5.0
    assert release._TIMEOUT.read <= 10.0


@respx.mock
def test_being_rate_limited_is_said_plainly(client):
    respx.get(LATEST).mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})
    )

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["ok"] is False
    assert "rate-limit" in body["reason"]


@respx.mock
def test_a_repository_with_no_published_release_says_so(client):
    """ไม่มี release และไม่มีแท็กที่อ่านเป็นเวอร์ชันได้ — อันนี้เท่านั้นที่ตอบว่าตรวจไม่ได้"""
    respx.get(LATEST).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    respx.get(TAGS).mock(return_value=httpx.Response(200, json=[]))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["ok"] is False
    assert "no published release" in body["reason"]


@respx.mock
def test_tags_answer_when_the_repo_has_never_published_a_release(client):
    """สภาพจริงของ repo นี้: แท็กครบทุกรุ่น แต่ไม่เคยกด Publish release สักครั้ง

    ถ้าไม่ถามต่อที่ `/tags` ปุ่มนี้จะตอบ "ยังไม่มี release" ตลอดไปและไม่มีวันเตือนใคร
    ได้เลย — ซึ่งคือทั้งหมดที่มันถูกสร้างมาเพื่อทำ
    """
    newer = _bump(config.VERSION)
    respx.get(LATEST).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    respx.get(TAGS).mock(return_value=httpx.Response(200, json=[
        {"name": f"v{config.VERSION}"},
        {"name": newer},          # `_bump` ใส่ "v" มาให้แล้ว
        {"name": "nightly"},
    ]))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["ok"] is True
    assert body["status"] == "behind"
    assert body["latest"] == newer
    assert body["release_url"].endswith(f"/releases/tag/{newer}")
    assert body["published_at"] == "", "แท็กเปล่า ๆ ไม่มีวันเผยแพร่ ต้องไม่เดาวันให้"


def test_the_newest_tag_is_the_highest_number_not_the_first_in_the_list():
    """GitHub เรียง `/tags` ตามตอนที่ ref ถูกสร้าง ไม่ใช่ตามเวอร์ชัน"""
    answer = httpx.Response(200, json=[
        {"name": "v1.9.0"}, {"name": "v1.12.1"}, {"name": "v1.10.0"},
        {"name": "v1.12"}, {"name": "release-2026"}, {"name": None}, "ไม่ใช่ dict",
    ])
    assert release._newest_tag(answer) == "v1.12.1"


def test_a_tag_list_that_cannot_be_read_is_not_turned_into_a_version():
    assert release._newest_tag(None) == ""
    assert release._newest_tag(httpx.Response(200, json=[{"name": "nightly"}])) == ""
    assert release._newest_tag(httpx.Response(200, text="not json")) == ""
    assert release._newest_tag(httpx.Response(500, json=[{"name": "v9.9.9"}])) == ""
    assert release._newest_tag(httpx.Response(200, json={"name": "v9.9.9"})) == ""


@respx.mock
def test_tags_are_asked_for_only_when_there_is_no_published_release(client):
    """คำขอที่สองต้องไม่เกิดตอนที่คำขอแรกตอบได้ — ปุ่มเดียวไม่ควรยิงสองที่โดยไม่จำเป็น"""
    respx.get(LATEST).mock(return_value=httpx.Response(
        200, json={"tag_name": f"v{config.VERSION}", "html_url": "https://example.invalid/r"}))
    tags = respx.get(TAGS).mock(return_value=httpx.Response(200, json=[]))

    client.post("/admin/version/check", headers=auth(client.admin_key))

    assert tags.call_count == 0
    assert respx.calls.call_count == 1


@respx.mock
def test_an_answer_without_a_tag_is_not_turned_into_a_version(client):
    respx.get(LATEST).mock(return_value=httpx.Response(200, json={"message": "hello"}))

    body = client.post("/admin/version/check", headers=auth(client.admin_key)).json()

    assert body["ok"] is False


# ---------------------------------------------------------------------------
# คำสั่งที่บอก ต้องตรงกับวิธีที่เครื่องนั้นถูกติดตั้ง
# ---------------------------------------------------------------------------
def test_a_checkout_and_a_copied_install_get_different_instructions(tmp_path):
    """สามแบบนี้อัปเดตคนละวิธีกันคนละเรื่อง — บอกผิดแบบคือบอกคำสั่งที่รันแล้วไม่เกิดอะไรขึ้น"""
    (tmp_path / ".git").mkdir()
    assert release.install_shape(tmp_path) in {"checkout", "container"}

    plain = tmp_path / "copied"
    plain.mkdir()
    assert release.install_shape(plain) in {"copied", "container"}

    assert (
        release.how_to_update("checkout")["commands"]
        != release.how_to_update("copied")["commands"]
    )


def test_the_copied_install_is_told_to_take_a_backup_first():
    """ขั้นตอนที่ข้ามแล้วเคยทำให้เครื่องล่ม — ห้ามหายไปจากคำแนะนำ"""
    steps = " ".join(release.how_to_update("copied")["commands"])
    assert "backup" in steps.lower()
    assert "compileall" in steps
    assert "import app.main" in steps


def test_a_container_is_told_to_rebuild_on_the_host():
    steps = release.how_to_update("container")
    assert "host" in " ".join(steps["commands"]).lower()
    assert "docker compose" in " ".join(steps["commands"])


# ---------------------------------------------------------------------------
# ฝั่งคอนโซล
# ---------------------------------------------------------------------------
def test_the_console_has_the_button_and_asks_only_when_it_is_pressed():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'id="ver-check"' in html
    assert js.count("'/admin/version/check'") == 1, "ต้องมีจุดเดียวที่เรียก endpoint นี้"
    press = js.index("$('ver-check').onclick")
    assert press < js.index("'/admin/version/check'"), "การเรียกต้องอยู่ในตัวจัดการการกดปุ่ม"


def test_the_browser_never_talks_to_anything_but_the_gateway():
    """ถ้าเบราว์เซอร์ยิง GitHub เอง IP ของคนเปิดคอนโซลจะไปโผล่ที่นั่นทุกครั้ง"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    literals = [m.group(2) for m in re.finditer(r"""fetch\(\s*(['"`])([^'"`]*)\1""", js)]
    assert literals, "อ่าน fetch ไม่เจอเลย — เทสนี้คงพังเงียบ ๆ"
    assert all(url.startswith("/") for url in literals), literals

    helper = re.compile(r"""\b(?:api|post|patch|del)\(\s*(['"`])([^'"`]*)\1""")
    calls = [m.group(2) for m in helper.finditer(js)]
    assert all(url.startswith("/") for url in calls), [u for u in calls if not u.startswith("/")]


def test_the_version_panel_is_admin_only():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'id="version-wrap"' in html
    at = html.index('id="version-wrap"')
    assert "hidden" in html[at - 120:at + 120]
    assert "$('version-wrap').hidden = !admin" in js


# ---------------------------------------------------------------------------
# ไม่มีใครเรียกเองนอกจากปุ่มนั้น
# ---------------------------------------------------------------------------
def test_the_only_caller_in_the_whole_app_is_the_admin_endpoint():
    """กันการถูกเติมเข้าไปใน startup, lifespan, health check หรือ task เบื้องหลัง"""
    callers = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if path.name != "release.py" and "release.check(" in path.read_text(encoding="utf-8")
    )
    assert callers == ["app/api/admin.py"], callers

    admin = (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8")
    assert admin.count("release.check(") == 1
    assert '@router.post("/version/check")' in admin
