"""Redis-backed fixed-window rate limiting for sensitive endpoints (§5.4).

A single :func:`rate_limiter` factory builds a FastAPI dependency from a setting
string of the form ``"<count>/<window_seconds>"`` (e.g. ``"5/60"``) and a
``bucket`` label. The dependency keys the counter by the caller's client IP
(the Cloudflare-set ``CF-Connecting-IP``, un-spoofable at the edge) plus the
bucket, so different endpoints and different clients never share a window.

The window is a plain ``INCR`` on a per-window key; ``EXPIRE`` is applied only on
the first hit (when the counter is created) so the window advances in fixed steps
and the key self-evicts. On overflow a ``429`` is raised with a domain
``rate_limited`` code — the unified HTTPException handler renders the envelope.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis

from app.core.redis import get_redis

_KEY_PREFIX = "ratelimit"
_CF_CONNECTING_IP_HEADER = "CF-Connecting-IP"
_UNKNOWN_CLIENT = "unknown"


def _parse_limit(limit_setting: str) -> tuple[int, int]:
    """Parse a ``"<count>/<window_seconds>"`` setting into its two integers.

    Args:
        limit_setting: The raw setting string (e.g. ``"5/60"``).

    Returns:
        tuple[int, int]: The allowed ``count`` and the ``window`` in seconds.

    Raises:
        ValueError: If the setting is malformed or non-positive.
    """
    count_raw, _, window_raw = limit_setting.partition("/")
    count = int(count_raw.strip())
    window = int(window_raw.strip())
    if count <= 0 or window <= 0:
        raise ValueError(
            f"rate-limit setting must be positive '<count>/<window>', got {limit_setting!r}"
        )
    return count, window


def _client_ip(request: Request) -> str:
    """Resolve the caller's IP for rate-limit keying, via ``CF-Connecting-IP``.

    Behind the Cloudflare Tunnel, ``CF-Connecting-IP`` is the authoritative
    client IP: Cloudflare sets it at the edge from the real connection and
    overwrites any client-supplied value, so it cannot be spoofed.
    ``X-Forwarded-For`` is deliberately **not** trusted — Cloudflare appends the
    real IP but leaves any client-supplied first hop in place, so keying on it
    would let a caller rotate a forged hop and bypass the limit entirely.

    Falls back to the direct connection peer (local dev / tests with no
    Cloudflare header), then to a sentinel when no source is available.

    Args:
        request: The incoming request.

    Returns:
        str: The client IP, or ``"unknown"`` when no source is available.
    """
    cf_ip = request.headers.get(_CF_CONNECTING_IP_HEADER)
    if cf_ip:
        cf_ip = cf_ip.strip()
        if cf_ip:
            return cf_ip
    if request.client and request.client.host:
        return request.client.host
    return _UNKNOWN_CLIENT


def rate_limiter(
    limit_setting: str,
    bucket: str,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a FastAPI dependency enforcing a fixed-window limit for ``bucket``.

    Args:
        limit_setting: The ``"<count>/<window_seconds>"`` budget for the window.
        bucket: A short label isolating this endpoint's counters (e.g.
            ``"login"``).

    Returns:
        Callable: An async dependency that raises ``429`` once the caller exceeds
        the budget within the window.
    """
    count, window = _parse_limit(limit_setting)

    async def _dependency(
        request: Request,
        redis: Redis = Depends(get_redis),
    ) -> None:
        """Increment the caller's window counter and reject on overflow."""
        client_ip = _client_ip(request)
        key = f"{_KEY_PREFIX}:{bucket}:{client_ip}"
        hits = await redis.incr(key)
        if hits == 1:
            await redis.expire(key, window)
        if hits > count:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "rate_limited",
                    "message": "Too many requests, please retry later.",
                },
            )

    return _dependency
