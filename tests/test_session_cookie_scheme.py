"""One gateway, two addresses, and the cookie that silently went missing.

This deployment answers on `https://host` and on `http://host:8080`. Cookies
are scoped by host, not by port or scheme, so both addresses shared one name.
The https cookie carries `Secure`, and a browser will not let an insecure page
overwrite a Secure cookie of the same name — it drops the new one and says
nothing.

The result was a sign-in that returned 200 and then answered every following
call with "No API key provided", which reads exactly like a wrong password and
is the one thing it was not. The user typed the right credentials three times
before reporting it.

A name per scheme means neither address can shadow the other.
"""

from __future__ import annotations

import pytest

from app.core.passwords import (
    SESSION_COOKIE,
    SESSION_COOKIE_INSECURE,
    read_session_cookie,
    session_cookie_name,
)

PASSWORD = "console-test-2026"


@pytest.fixture
def console(temp_db, monkeypatch):
    """A client signed in the way the console signs in - cookie, not bearer.

    The bootstrap password is random and only printed to the log, so pin it
    through the environment instead of scraping it back out.
    """
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("GW_ADMIN_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def sign_in(console):
    return console.post("/auth/login", json={"username": "admin", "password": PASSWORD})


def test_the_two_schemes_do_not_share_a_name():
    """ชื่อซ้ำกันเมื่อไร เบราว์เซอร์จะทิ้งใบใหม่เงียบ ๆ ทันที"""
    assert session_cookie_name(secure=True) != session_cookie_name(secure=False)


def test_each_scheme_reads_its_own():
    jar = {SESSION_COOKIE: "from-https", SESSION_COOKIE_INSECURE: "from-http"}
    assert read_session_cookie(jar, secure=True) == "from-https"
    assert read_session_cookie(jar, secure=False) == "from-http"


def test_the_other_name_is_accepted_when_its_own_is_absent():
    """หลัง proxy ที่ปิด TLS ให้ request มาถึงเป็น https ทั้งที่คุกกี้เก็บอีกชื่อ

    ถ้าไม่ยอมรับข้ามชื่อ คนใช้จะโดนให้ล็อกอินซ้ำโดยไม่มีเหตุผลที่อธิบายได้
    """
    assert read_session_cookie({SESSION_COOKIE: "x"}, secure=False) == "x"
    assert read_session_cookie({SESSION_COOKIE_INSECURE: "y"}, secure=True) == "y"


def test_no_cookie_is_the_empty_string_not_none():
    assert read_session_cookie({}, secure=True) == ""


def test_signing_in_over_plain_http_sets_a_cookie_that_is_not_secure(console):
    """คุกกี้ Secure จะไม่ถูกส่งกลับมาบน http เลย = ล็อกอินแล้วเหมือนไม่ได้ล็อกอิน"""
    response = sign_in(console)
    assert response.status_code == 200, response.text

    header = response.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE_INSECURE}=")
    assert "Secure" not in header
    assert "HttpOnly" in header


def test_the_cookie_actually_carries_the_session(console):
    sign_in(console)
    status = console.get("/auth/status").json()
    assert status["session"], "ล็อกอินผ่านแต่ session ไม่ติด = อาการที่ผู้ใช้เจอ"

    me = console.get("/v1/me")
    assert me.status_code == 200, "call ถัดจากล็อกอินต้องมี credential ติดไปด้วย"


def test_signing_out_clears_both_names(console):
    """ออกจากระบบทางหนึ่งต้องไม่ทิ้ง session ค้างไว้บนอีกที่อยู่ของ gateway เดียวกัน"""
    sign_in(console)
    response = console.post("/auth/logout")

    cleared = response.headers.get_list("set-cookie")
    assert any(c.startswith(f"{SESSION_COOKIE}=") for c in cleared)
    assert any(c.startswith(f"{SESSION_COOKIE_INSECURE}=") for c in cleared)
    assert not console.get("/auth/status").json()["session"]
