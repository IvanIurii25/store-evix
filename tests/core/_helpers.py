"""Shared low-level constructors for the cross-cutting core tests.

These build raw Starlette ``Request`` objects (no live server) so the auth /
lang / rate-limit dependency functions can be invoked directly with crafted
headers, cookies and client IPs — the fastest, most isolated way to hit their
branches.
"""

from __future__ import annotations

from starlette.requests import Request

_HTTP_VERSION = "1.1"
_ASGI_HTTP = "http"


def build_request(
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    query_string: str = "",
    client: tuple[str, int] | None = ("127.0.0.1", 12345),
) -> Request:
    """Construct a bare ``Request`` with the given headers, cookies and client.

    Args:
        headers: Header name -> value pairs (case-insensitive on read).
        cookies: Cookie name -> value pairs, rendered into a ``Cookie`` header.
        query_string: Raw query string (without the leading ``?``).
        client: ``(host, port)`` tuple, or ``None`` for an absent client.

    Returns:
        Request: A minimal, self-contained request for direct dependency calls.
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_header.encode("latin-1")))

    scope = {
        "type": _ASGI_HTTP,
        "http_version": _HTTP_VERSION,
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": query_string.encode("latin-1"),
        "headers": raw_headers,
        "client": client,
        "server": ("test", 80),
        "scheme": "http",
    }
    return Request(scope)
