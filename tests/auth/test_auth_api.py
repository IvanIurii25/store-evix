"""API-level tests for the auth + users routers (B4 DoD)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import AppUser

pytestmark = pytest.mark.asyncio

_PASSWORD = "s3cret-pass"


async def _register(client: AsyncClient, email: str, phone: str | None = None) -> None:
    """Register a user and assert 201."""
    payload = {"email": email, "password": _PASSWORD}
    if phone:
        payload["phone"] = phone
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 201, resp.text


async def _login(client: AsyncClient, email: str) -> dict:
    """Log in and return the token pair body."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": _PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_register_then_duplicate_email(client: AsyncClient) -> None:
    """Registering the same email twice returns 409 in the unified envelope."""
    await _register(client, "dup@example.com")
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "dup@example.com", "password": _PASSWORD},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "http_error"


async def test_register_duplicate_email_case_insensitive(client: AsyncClient) -> None:
    """CITEXT: a different-case email is still a duplicate (409)."""
    await _register(client, "Case@Example.com")
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "case@example.com", "password": _PASSWORD},
    )
    assert resp.status_code == 409


async def test_login_correct_password(client: AsyncClient) -> None:
    """Valid credentials return an access + refresh pair."""
    await _register(client, "login@example.com")
    tokens = await _login(client, "login@example.com")
    assert tokens["access"]
    assert tokens["refresh"]


async def test_login_wrong_password_401(client: AsyncClient) -> None:
    """A wrong password returns 401 in the unified error envelope."""
    await _register(client, "badpass@example.com")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "badpass@example.com", "password": "not-it"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Invalid credentials"


async def test_login_sets_httponly_cookies(client: AsyncClient) -> None:
    """Login sets httpOnly access + refresh cookies."""
    await _register(client, "cookie@example.com")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "cookie@example.com", "password": _PASSWORD},
    )
    set_cookie = resp.headers.get_list("set-cookie")
    joined = " ".join(set_cookie).lower()
    assert "access=" in joined
    assert "refresh=" in joined
    assert "httponly" in joined


async def test_login_by_phone(client: AsyncClient) -> None:
    """Login works with phone as the identifier."""
    await _register(client, "phone@example.com", phone="+37360000000")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"phone": "+37360000000", "password": _PASSWORD},
    )
    assert resp.status_code == 200, resp.text


async def test_password_hashed_in_db(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The stored password is an argon2 hash, never the plaintext."""
    await _register(client, "hash@example.com")
    stmt = select(AppUser).where(AppUser.email == "hash@example.com")
    user = (await db_session.execute(stmt)).scalar_one()
    assert user.password_hash != _PASSWORD
    assert user.password_hash.startswith("$argon2")


async def test_refresh_rotates(client: AsyncClient) -> None:
    """Refresh issues a new pair (rotation)."""
    await _register(client, "rotate@example.com")
    tokens = await _login(client, "rotate@example.com")
    resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh": tokens["refresh"]},
    )
    assert resp.status_code == 200, resp.text
    new_tokens = resp.json()
    assert new_tokens["refresh"] != tokens["refresh"]


async def test_old_refresh_rejected_after_rotation(client: AsyncClient) -> None:
    """The rotated-out refresh token is blacklisted and rejected."""
    await _register(client, "reuse@example.com")
    tokens = await _login(client, "reuse@example.com")
    first = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh": tokens["refresh"]},
    )
    assert first.status_code == 200
    replay = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh": tokens["refresh"]},
    )
    assert replay.status_code == 401


async def test_logout_blacklists_refresh(client: AsyncClient) -> None:
    """After logout the refresh token can no longer be used."""
    await _register(client, "logout@example.com")
    tokens = await _login(client, "logout@example.com")
    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refresh": tokens["refresh"]},
    )
    assert logout.status_code == 204
    resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh": tokens["refresh"]},
    )
    assert resp.status_code == 401


async def test_me_without_token_401(client: AsyncClient) -> None:
    """GET /users/me without a token returns 401."""
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401


async def test_me_with_token_200(client: AsyncClient) -> None:
    """GET /users/me with a valid bearer token returns the profile."""
    await _register(client, "me@example.com")
    tokens = await _login(client, "me@example.com")
    resp = await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {tokens['access']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "me@example.com"


async def test_address_crud_single_default(client: AsyncClient) -> None:
    """Address CRUD works and only one address stays default per user."""
    await _register(client, "addr@example.com")
    tokens = await _login(client, "addr@example.com")
    auth = {"Authorization": f"Bearer {tokens['access']}"}

    first = await client.post(
        "/api/v1/users/me/addresses",
        headers=auth,
        json={
            "full_name": "Ion Popescu",
            "phone": "+37360000001",
            "city": "Chisinau",
            "street": "Stefan cel Mare 1",
            "is_default": True,
        },
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = await client.post(
        "/api/v1/users/me/addresses",
        headers=auth,
        json={
            "full_name": "Maria Popescu",
            "phone": "+37360000002",
            "city": "Balti",
            "street": "Independentei 5",
            "is_default": True,
        },
    )
    assert second.status_code == 201, second.text

    listed = await client.get("/api/v1/users/me/addresses", headers=auth)
    assert listed.status_code == 200
    addresses = listed.json()
    defaults = [addr for addr in addresses if addr["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] != first_id

    delete = await client.delete(
        f"/api/v1/users/me/addresses/{first_id}",
        headers=auth,
    )
    assert delete.status_code == 204
    remaining = await client.get("/api/v1/users/me/addresses", headers=auth)
    assert len(remaining.json()) == 1


async def test_update_default_via_patch(client: AsyncClient) -> None:
    """PATCH setting is_default flips the default, keeping exactly one."""
    await _register(client, "patchdef@example.com")
    tokens = await _login(client, "patchdef@example.com")
    auth = {"Authorization": f"Bearer {tokens['access']}"}

    base = {
        "phone": "+37360000003",
        "city": "Chisinau",
        "street": "Test 1",
        "full_name": "A",
    }
    first = await client.post(
        "/api/v1/users/me/addresses",
        headers=auth,
        json={**base, "is_default": True},
    )
    second = await client.post(
        "/api/v1/users/me/addresses",
        headers=auth,
        json={**base, "full_name": "B", "is_default": False},
    )
    second_id = second.json()["id"]

    patched = await client.patch(
        f"/api/v1/users/me/addresses/{second_id}",
        headers=auth,
        json={"is_default": True},
    )
    assert patched.status_code == 200, patched.text

    listed = await client.get("/api/v1/users/me/addresses", headers=auth)
    defaults = [addr for addr in listed.json() if addr["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == second_id
    assert first.json()["id"] != second_id
