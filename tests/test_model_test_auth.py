"""Running the model suite from the console, which is the only place the button is.

The suite drives the public API, so it has to carry a credential. It read the
caller's bearer token and nothing else — correct for a program calling with a
key, and empty for the console, which authenticates with a session cookie. Every
run started from the UI came back the same way:

    MISSING_API_KEY: No API key provided.

The public API accepts either credential; the suite just was not passing the one
the console had.
"""

from __future__ import annotations

from app.core.modeltest import ModelTestSuite
from app.core.passwords import SESSION_COOKIE


def test_a_session_cookie_is_carried_into_the_suite():
    suite = ModelTestSuite("http://gw", "", "coding", session_cookie="abc123")
    assert suite._client.cookies.get(SESSION_COOKIE) == "abc123"
    assert "Authorization" not in suite._client.headers


def test_a_bearer_token_still_works_on_its_own():
    suite = ModelTestSuite("http://gw", "lg_sk_test", "coding")
    assert suite._client.headers["Authorization"] == "Bearer lg_sk_test"
    assert not suite._client.cookies


def test_neither_credential_sends_neither_header_nor_cookie():
    """ก่อนหน้านี้ส่ง 'Bearer ' เปล่า ๆ ซึ่งดูเหมือนมี credential ทั้งที่ไม่มี"""
    suite = ModelTestSuite("http://gw", "", "coding")
    assert "Authorization" not in suite._client.headers
    assert not suite._client.cookies
