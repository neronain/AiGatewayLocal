"""Console sign-in, roles, and self-service API keys."""

from __future__ import annotations

import asyncio

import pytest

from app.core.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordError,
    hash_password,
    issue_session,
    read_session,
    verify_password,
)

GOOD_PASSWORD = "correct-horse-battery"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def test_password_round_trip():
    stored = hash_password(GOOD_PASSWORD)
    assert stored.startswith("scrypt$")
    assert GOOD_PASSWORD not in stored  # never recoverable from the hash
    assert verify_password(GOOD_PASSWORD, stored)
    assert not verify_password(GOOD_PASSWORD + "x", stored)


def test_short_passwords_are_refused():
    with pytest.raises(PasswordError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


def test_an_account_without_a_password_can_never_sign_in():
    assert not verify_password("", "")
    assert not verify_password("anything", "")


def test_session_token_is_tamper_evident():
    token = issue_session("user-1", 0)
    assert read_session(token)["sub"] == "user-1"

    body, signature = token.split(".", 1)
    forged = issue_session("user-2", 0).split(".", 1)[0] + "." + signature
    assert read_session(forged) is None
    assert read_session("nonsense") is None


def test_expired_sessions_are_rejected():
    assert read_session(issue_session("user-1", 0, ttl=-1)) is None


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
def _make_user(role: str, username: str, password: str | None = GOOD_PASSWORD) -> str:
    from app.db.models import User
    from app.db.session import session_scope

    async def create() -> str:
        async with session_scope() as session:
            user = User(
                external_id=username,
                display_name=username.title(),
                role=role,
                password_hash=hash_password(password) if password else "",
            )
            session.add(user)
            await session.flush()
            return user.id

    return asyncio.run(create())


def test_status_reports_a_configured_instance(client):
    body = client.get("/auth/status").json()
    # conftest creates an admin, so setup is closed.
    assert body["needs_setup"] is False
    assert body["session"] is None


def test_setup_is_closed_once_an_admin_exists(client):
    response = client.post(
        "/auth/setup",
        json={"username": "second", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 403


def test_login_and_session_identifies_the_caller(client):
    _make_user("member", "somchai")
    response = client.post(
        "/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "member"
    assert "litegate_session" in response.cookies

    # The cookie alone now authenticates - no API key involved.
    me = client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["external_id"] == "somchai"


def test_wrong_password_and_unknown_user_look_identical(client):
    _make_user("member", "somchai")
    unknown = client.post(
        "/auth/login", json={"username": "nobody", "password": GOOD_PASSWORD}
    )
    wrong = client.post(
        "/auth/login", json={"username": "somchai", "password": "not-the-password"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


def test_suspended_accounts_cannot_sign_in(client):
    from app.db.models import User
    from app.db.session import session_scope

    user_id = _make_user("member", "gone")

    async def suspend() -> None:
        async with session_scope() as session:
            user = await session.get(User, user_id)
            user.status = "suspended"

    asyncio.run(suspend())
    response = client.post(
        "/auth/login", json={"username": "gone", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 403


def test_logout_clears_the_session(client):
    _make_user("member", "somchai")
    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})
    assert client.get("/v1/me").status_code == 200
    client.post("/auth/logout")
    client.cookies.clear()
    assert client.get("/v1/me").status_code == 401


def test_changing_a_password_signs_other_sessions_out(client):
    _make_user("member", "somchai")
    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})
    stolen = client.cookies.get("litegate_session")

    changed = client.post(
        "/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": "a-brand-new-secret"},
    )
    assert changed.status_code == 200

    # The caller keeps working on the session they changed it from.
    assert client.get("/v1/me").status_code == 200

    # A session captured before the change no longer works.
    client.cookies.clear()
    client.cookies.set("litegate_session", stolen)
    assert client.get("/v1/me").status_code == 401


def test_password_cannot_be_changed_with_an_api_key(client, member_key):
    """A leaked key must not be able to lock the owner out of their account."""
    response = client.post(
        "/auth/password",
        headers=auth(member_key),
        json={"current_password": GOOD_PASSWORD, "new_password": "another-long-secret"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Self-service keys
# ---------------------------------------------------------------------------
def test_a_member_can_issue_and_revoke_their_own_key(client):
    _make_user("member", "somchai")
    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})

    issued = client.post("/v1/me/api-keys", json={"name": "laptop"})
    assert issued.status_code == 201
    body = issued.json()
    assert body["api_key"].startswith("lg_sk_")

    # The new key works for the API straight away.
    client.cookies.clear()
    assert client.get("/v1/me", headers=auth(body["api_key"])).status_code == 200

    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})
    listed = client.get("/v1/me/api-keys").json()
    assert [k["name"] for k in listed["data"]] == ["laptop"]
    assert listed["active"] == 1

    client.delete(f"/v1/me/api-keys/{body['id']}")
    client.cookies.clear()
    assert client.get("/v1/me", headers=auth(body["api_key"])).status_code == 401


def test_self_service_keys_are_capped(client):
    _make_user("member", "somchai")
    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})

    limit = client.get("/v1/me/api-keys").json()["limit"]
    for index in range(limit):
        assert client.post("/v1/me/api-keys", json={"name": f"key{index}"}).status_code == 201

    refused = client.post("/v1/me/api-keys", json={"name": "one too many"})
    assert refused.status_code == 400
    assert refused.json()["error"]["details"]["limit"] == limit

    # Revoking one frees a slot.
    first = client.get("/v1/me/api-keys").json()["data"][-1]["id"]
    client.delete(f"/v1/me/api-keys/{first}")
    assert client.post("/v1/me/api-keys", json={"name": "replacement"}).status_code == 201


def test_an_api_key_cannot_mint_another_key(client, member_key):
    response = client.post("/v1/me/api-keys", headers=auth(member_key), json={})
    assert response.status_code == 403


def test_members_cannot_revoke_someone_elses_key(client, member_key):
    _make_user("member", "somchai")
    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})
    mine = client.post("/v1/me/api-keys", json={"name": "mine"}).json()["id"]

    _make_user("member", "other")
    client.cookies.clear()
    client.post("/auth/login", json={"username": "other", "password": GOOD_PASSWORD})

    # Same answer as a key that does not exist: no probing other people's ids.
    assert client.delete(f"/v1/me/api-keys/{mine}").status_code == 400


# ---------------------------------------------------------------------------
# Role boundaries
# ---------------------------------------------------------------------------
def test_a_member_cannot_touch_the_registry_or_other_people(client):
    _make_user("member", "somchai")
    client.post("/auth/login", json={"username": "somchai", "password": GOOD_PASSWORD})

    assert client.get("/admin/models").status_code == 403
    assert client.get("/admin/users").status_code == 403
    assert client.post("/admin/registry/reload").status_code == 403


def test_a_manager_manages_people_but_not_the_registry(client):
    """The line: managers manage who may use what; admins change what exists."""
    _make_user("manager", "ajarn")
    client.post("/auth/login", json={"username": "ajarn", "password": GOOD_PASSWORD})

    # People and workspaces: allowed.
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/workspaces").status_code == 200
    assert client.get("/admin/api-keys").status_code == 200

    # The registry and the machines behind it: not allowed.
    assert client.get("/admin/models").status_code == 403
    assert client.post("/admin/registry/reload").status_code == 403
    assert client.get("/admin/models/coding/advice").status_code == 403
    assert client.post("/admin/models/coding/test").status_code == 403


def test_an_admin_can_do_both(client):
    _make_user("admin", "root-user")
    client.post("/auth/login", json={"username": "root-user", "password": GOOD_PASSWORD})

    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/models").status_code == 200
    assert client.post("/admin/registry/reload").status_code == 200
