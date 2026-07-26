"""Unit tests for :class:`~app.services.payment.maib_client.MaibClient`.

The maib HTTP calls are mocked with an ``httpx.MockTransport`` injected by
monkeypatching the ``httpx.AsyncClient`` the client constructs, so no network is
touched. Signature tests compute the expected value with the client's *own*
algorithm (per the spec) rather than a hardcoded constant, so they stay correct
regardless of scalar-serialization details.
"""

import base64
import hashlib

import httpx
import pytest

from app.services.payment import maib_client as maib_module
from app.services.payment.maib_client import MaibClient, MaibError

pytestmark = pytest.mark.asyncio

_SIGNATURE_KEY = "unit-sig-key"


def _client() -> MaibClient:
    """Build a client with fixed credentials (no settings dependency)."""
    return MaibClient(
        base_url="https://maib.test/v1",
        project_id="proj",
        project_secret="secret",
        signature_key=_SIGNATURE_KEY,
    )


def _install_transport(monkeypatch, handler) -> None:
    """Route the client's internal ``httpx.AsyncClient`` through ``handler``."""
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(maib_module.httpx, "AsyncClient", _factory)


def _reference_signature(result: dict) -> str:
    """Recompute the maib signature independently to cross-check the client.

    Mirrors the documented algorithm: recursively sort keys, append the key,
    join values with ``":"``, base64(sha256).
    """

    def flatten(node):
        if isinstance(node, dict):
            out = []
            for key in sorted(node.keys()):
                out.extend(flatten(node[key]))
            return out
        if isinstance(node, (list, tuple)):
            out = []
            for item in node:
                out.extend(flatten(item))
            return out
        if node is None:
            return [""]
        if isinstance(node, bool):
            return ["true" if node else "false"]
        return [str(node)]

    payload = ":".join(flatten(result) + [_SIGNATURE_KEY])
    return base64.b64encode(hashlib.sha256(payload.encode()).digest()).decode()


async def test_get_token_caches_and_reuses(monkeypatch) -> None:
    """A second ``get_token`` within TTL does not re-hit ``/generate-token``."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "accessToken": "tok-1",
                "expiresIn": 3600,
                "refreshToken": "ref-1",
                "refreshExpiresIn": 7200,
            },
        )

    _install_transport(monkeypatch, handler)
    client = _client()

    assert await client.get_token() == "tok-1"
    assert await client.get_token() == "tok-1"
    assert calls["n"] == 1, "token must be cached, not re-minted"


async def test_get_token_error_raises_maib_error(monkeypatch) -> None:
    """A non-2xx token response surfaces as ``MaibError``."""
    _install_transport(
        monkeypatch, lambda req: httpx.Response(401, json={"error": "nope"})
    )
    with pytest.raises(MaibError):
        await _client().get_token()


async def test_create_payment_returns_pay_url(monkeypatch) -> None:
    """``create_payment`` returns the maib ``payId`` / ``payUrl`` block."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generate-token"):
            return httpx.Response(
                200, json={"accessToken": "tok", "expiresIn": 3600}
            )
        assert request.url.path.endswith("/pay")
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "result": {
                    "payId": "PAY-9",
                    "payUrl": "https://maib.test/checkout/PAY-9",
                    "orderId": "EVX-1",
                }
            },
        )

    _install_transport(monkeypatch, handler)
    result = await _client().create_payment(
        amount=100.0,
        order_id="EVX-1",
        client_ip="1.2.3.4",
        language="ro",
        description="Order EVX-1",
        callback_url="https://api.test/api/v1/payments/maib/callback",
        ok_url="https://shop.test/ro/checkout/success?order=EVX-1",
        fail_url="https://shop.test/ro/checkout/failed?order=EVX-1",
    )
    assert result["payId"] == "PAY-9"
    assert result["payUrl"].endswith("/PAY-9")


async def test_verify_signature_valid(monkeypatch) -> None:
    """A correctly-signed result verifies true."""
    result = {
        "payId": "PAY-1",
        "orderId": "EVX-1",
        "status": "OK",
        "amount": 150.0,
        "currency": "MDL",
    }
    signature = _reference_signature(result)
    assert _client().verify_callback_signature(result, signature) is True


async def test_verify_signature_invalid(monkeypatch) -> None:
    """A tampered result (or wrong signature) verifies false."""
    result = {"payId": "PAY-1", "status": "OK", "amount": 150.0}
    good = _reference_signature(result)
    # Tamper the amount after signing → signature must no longer match.
    tampered = {**result, "amount": 999.0}
    assert _client().verify_callback_signature(tampered, good) is False
    assert _client().verify_callback_signature(result, "not-a-signature") is False
