"""Behavioural tests for the Redis fixed-window rate limiter (W7, §5.4).

Covers, per endpoint (login ``5/60``, checkout ``10/60``):

* the first ``N`` requests within the window pass the limiter (any non-429
  status from the handler is fine — the point is the limiter does not fire);
* the ``(N+1)``-th request is rejected with ``429`` / ``rate_limited``;
* two different client IPs keep independent windows;
* expiring the window key (no real wait) lets the caller through again.

Login is driven with deliberately wrong credentials: the limiter runs as a route
dependency *before* the handler, so its firing is observable regardless of the
auth outcome. Checkout is driven as a guest with an empty cart for the same
reason.
"""

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.config import settings

# --- endpoint under test parametrisation -----------------------------------

_LOGIN_LIMIT = int(settings.rate_limit_login.split("/")[0])
_CHECKOUT_LIMIT = int(settings.rate_limit_checkout.split("/")[0])

_LOGIN_PATH = "/api/v1/auth/login"
_LOGIN_BODY = {"email": "nobody@example.com", "password": "wrong-password"}
_LOGIN_BUCKET = "login"

_CHECKOUT_PATH = "/api/v1/checkout"
_CHECKOUT_BODY = {
    "email": "guest@example.com",
    "phone": "+37360000000",
    "delivery_type": "pickup",
}
_CHECKOUT_BUCKET = "checkout"

_CASES = [
    pytest.param(_LOGIN_PATH, _LOGIN_BODY, _LOGIN_LIMIT, _LOGIN_BUCKET, id="login"),
    pytest.param(
        _CHECKOUT_PATH, _CHECKOUT_BODY, _CHECKOUT_LIMIT, _CHECKOUT_BUCKET, id="checkout"
    ),
]

_RATE_LIMITED = 429


async def _post(
    client: AsyncClient,
    path: str,
    body: dict,
    *,
    client_ip: str,
) -> int:
    """POST ``body`` to ``path`` as a fixed client IP; return the status code."""
    resp = await client.post(
        path, json=body, headers={"CF-Connecting-IP": client_ip}
    )
    return resp.status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body", "limit", "bucket"), _CASES)
async def test_limit_allows_up_to_n_then_rejects(
    client: AsyncClient,
    path: str,
    body: dict,
    limit: int,
    bucket: str,
) -> None:
    """First ``limit`` requests pass the limiter; the next is ``429 rate_limited``."""
    ip = "203.0.113.10"
    for _ in range(limit):
        status_code = await _post(client, path, body, client_ip=ip)
        assert status_code != _RATE_LIMITED

    resp = await client.post(path, json=body, headers={"CF-Connecting-IP": ip})
    assert resp.status_code == _RATE_LIMITED
    assert resp.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body", "limit", "bucket"), _CASES)
async def test_distinct_ips_have_independent_windows(
    client: AsyncClient,
    path: str,
    body: dict,
    limit: int,
    bucket: str,
) -> None:
    """One IP hitting its ceiling does not throttle a different IP."""
    ip_hot = "203.0.113.20"
    ip_cold = "203.0.113.21"

    for _ in range(limit):
        assert await _post(client, path, body, client_ip=ip_hot) != _RATE_LIMITED
    assert await _post(client, path, body, client_ip=ip_hot) == _RATE_LIMITED

    # A separate IP starts from an empty window.
    assert await _post(client, path, body, client_ip=ip_cold) != _RATE_LIMITED


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body", "limit", "bucket"), _CASES)
async def test_window_reset_lets_caller_through_again(
    client: AsyncClient,
    redis_client: Redis,
    path: str,
    body: dict,
    limit: int,
    bucket: str,
) -> None:
    """Dropping the window key (simulated expiry) re-opens the budget."""
    ip = "203.0.113.30"
    key = f"ratelimit:{bucket}:{ip}"

    for _ in range(limit):
        assert await _post(client, path, body, client_ip=ip) != _RATE_LIMITED
    assert await _post(client, path, body, client_ip=ip) == _RATE_LIMITED

    # Simulate the fixed window elapsing without real waiting.
    await redis_client.delete(key)

    assert await _post(client, path, body, client_ip=ip) != _RATE_LIMITED


@pytest.mark.asyncio
async def test_register_is_rate_limited(client: AsyncClient) -> None:
    """Registration trips ``429 rate_limited`` once its per-IP budget is spent."""
    limit = int(settings.rate_limit_register.split("/")[0])
    ip = "203.0.113.50"
    body = {"email": "reg-flood@example.com", "password": "password123"}

    for _ in range(limit):
        code = await _post(client, "/api/v1/auth/register", body, client_ip=ip)
        assert code != _RATE_LIMITED

    resp = await client.post(
        "/api/v1/auth/register", json=body, headers={"CF-Connecting-IP": ip}
    )
    assert resp.status_code == _RATE_LIMITED
    assert resp.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_refresh_is_rate_limited(client: AsyncClient) -> None:
    """Refresh trips ``429 rate_limited`` once its per-IP budget is spent."""
    limit = int(settings.rate_limit_refresh.split("/")[0])
    ip = "203.0.113.60"
    body = {"refresh": "invalid-token"}

    for _ in range(limit):
        code = await _post(client, "/api/v1/auth/refresh", body, client_ip=ip)
        assert code != _RATE_LIMITED

    resp = await client.post(
        "/api/v1/auth/refresh", json=body, headers={"CF-Connecting-IP": ip}
    )
    assert resp.status_code == _RATE_LIMITED
    assert resp.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_login_and_checkout_buckets_are_isolated(client: AsyncClient) -> None:
    """Exhausting the login window does not consume the checkout budget."""
    ip = "203.0.113.40"
    headers = {"CF-Connecting-IP": ip}

    for _ in range(_LOGIN_LIMIT):
        await client.post(_LOGIN_PATH, json=_LOGIN_BODY, headers=headers)
    login_over = await client.post(_LOGIN_PATH, json=_LOGIN_BODY, headers=headers)
    assert login_over.status_code == _RATE_LIMITED

    # Same IP, different bucket -> still allowed.
    checkout_resp = await client.post(
        _CHECKOUT_PATH, json=_CHECKOUT_BODY, headers=headers
    )
    assert checkout_resp.status_code != _RATE_LIMITED
