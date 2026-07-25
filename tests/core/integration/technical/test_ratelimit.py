"""Unit tests for the Redis fixed-window limiter in :mod:`app.api.ratelimit`.

The ``rate_limiter`` factory's inner ``_dependency`` is invoked directly with a
crafted ``Request`` and an isolated Redis client, so the allow / block window
transition and the client-IP resolution branches are exercised without an ASGI
round-trip.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from app.api.ratelimit import (
    _UNKNOWN_CLIENT,
    _client_ip,
    _parse_limit,
    rate_limiter,
)
from tests.core._helpers import build_request

# A 2-request budget makes the block boundary (3rd call) cheap to assert.
_LIMIT_SETTING = "2/60"
_BUCKET = "core-test"


class TestRateLimitParse:
    """``_parse_limit`` — valid split and the non-positive guard."""

    def test_parse_valid_setting_returns_count_and_window(self):
        # Arrange / Act: a well-formed budget string.
        count, window = _parse_limit("5/60")

        # Assert: both integers are parsed.
        assert (count, window) == (5, 60), "'5/60' must parse to (5, 60)"

    def test_parse_non_positive_count_raises_value_error(self):
        # Arrange / Act / Assert: a zero count is rejected.
        with pytest.raises(ValueError):
            _parse_limit("0/60")

    def test_parse_non_positive_window_raises_value_error(self):
        # Arrange / Act / Assert: a zero window is rejected.
        with pytest.raises(ValueError):
            _parse_limit("5/0")


class TestRateLimitClientIp:
    """``_client_ip`` — CF-Connecting-IP precedence and fallbacks."""

    def test_client_ip_uses_cf_connecting_ip(self):
        # Arrange: a request carrying the Cloudflare-set client IP.
        request = build_request(headers={"CF-Connecting-IP": "203.0.113.7"})

        # Act.
        resolved = _client_ip(request)

        # Assert: the Cloudflare-authoritative IP is used.
        assert resolved == "203.0.113.7", "CF-Connecting-IP must be used"

    def test_client_ip_ignores_forwarded_for(self):
        # Arrange: only a (spoofable) X-Forwarded-For, plus a real peer client.
        request = build_request(
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            client=("198.51.100.9", 5000),
        )

        # Act.
        resolved = _client_ip(request)

        # Assert: XFF is not trusted — the connection peer wins instead.
        assert resolved == "198.51.100.9", (
            "X-Forwarded-For must not be trusted for rate-limit keying"
        )

    def test_client_ip_blank_cf_header_falls_back_to_peer(self):
        # Arrange: an empty CF-Connecting-IP value + a real peer client.
        request = build_request(
            headers={"CF-Connecting-IP": "   "}, client=("198.51.100.9", 5000)
        )

        # Act.
        resolved = _client_ip(request)

        # Assert: a blank CF header falls back to the connection peer.
        assert resolved == "198.51.100.9", (
            "a blank CF-Connecting-IP must fall back to peer"
        )

    def test_client_ip_no_source_returns_unknown(self):
        # Arrange: no CF header and no client on the scope.
        request = build_request(client=None)

        # Act.
        resolved = _client_ip(request)

        # Assert: with no source at all, the sentinel is returned.
        assert resolved == _UNKNOWN_CLIENT, (
            "no IP source must yield the unknown sentinel"
        )


class TestRateLimitDependency:
    """The built ``_dependency`` — allow under budget, block at overflow."""

    async def test_dependency_allows_calls_within_budget(self, redis_client):
        # Arrange: a 2/60 limiter dependency and a fixed-IP request.
        dependency = rate_limiter(_LIMIT_SETTING, _BUCKET)
        request = build_request(headers={"CF-Connecting-IP": "192.0.2.1"})

        # Act / Assert: the first two calls (== budget) do not raise.
        await dependency(request, redis_client)
        await dependency(request, redis_client)

    async def test_dependency_blocks_call_over_budget_with_429(self, redis_client):
        # Arrange: same 2/60 limiter, three calls from one IP.
        dependency = rate_limiter(_LIMIT_SETTING, _BUCKET)
        request = build_request(headers={"CF-Connecting-IP": "192.0.2.2"})
        await dependency(request, redis_client)
        await dependency(request, redis_client)

        # Act / Assert: the third call overflows the window with 429.
        with pytest.raises(HTTPException) as exc_info:
            await dependency(request, redis_client)
        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS, (
            "a call over the budget must raise 429"
        )
