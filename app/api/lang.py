"""Language-resolution dependency for catalog reads (§2.1.1).

Resolves the request language from, in priority order:

1. the ``?lang=`` query parameter,
2. the first supported token of the ``Accept-Language`` header,
3. the default (``ro``).

The resolved value is validated against :data:`app.models.catalog.ALLOWED_LANGS`;
an explicit unsupported ``?lang=`` is rejected with 400 (unified envelope via the
app's HTTPException handler).
"""

from fastapi import HTTPException, Query, status
from starlette.requests import Request

from app.models.catalog import ALLOWED_LANGS

DEFAULT_LANG: str = "ro"


def _parse_accept_language(header: str | None) -> str | None:
    """Return the first supported language token from an ``Accept-Language`` header.

    Args:
        header: Raw header value (may be ``None``).

    Returns:
        str | None: A supported language code, or ``None`` if none matched.
    """
    if not header:
        return None
    for part in header.split(","):
        token = part.split(";", 1)[0].strip().lower()
        # Normalize e.g. "ru-RU" -> "ru".
        base = token.split("-", 1)[0]
        if base in ALLOWED_LANGS:
            return base
    return None


def get_lang(
    request: Request,
    lang: str | None = Query(default=None, description="Language code (ru|ro)."),
) -> str:
    """Resolve and validate the request language (§2.1.1).

    Args:
        request: Incoming request (for the ``Accept-Language`` header).
        lang: Optional explicit ``?lang=`` override.

    Returns:
        str: A validated language code from :data:`ALLOWED_LANGS`.

    Raises:
        HTTPException: 400 if an explicit ``?lang=`` value is unsupported.
    """
    if lang is not None:
        normalized = lang.strip().lower()
        if normalized not in ALLOWED_LANGS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported language '{lang}'. Allowed: {list(ALLOWED_LANGS)}",
            )
        return normalized

    header_lang = _parse_accept_language(request.headers.get("accept-language"))
    if header_lang is not None:
        return header_lang
    return DEFAULT_LANG


def normalize_lang(lang: str | None) -> str:
    """Coerce an arbitrary language input to a supported code, never raising.

    Unlike :func:`get_lang` (which rejects an unsupported explicit ``?lang=`` with
    400), this is a lenient boundary normalizer for the cart / checkout stack: it
    accepts any supported value in :data:`ALLOWED_LANGS` and silently falls back to
    :data:`DEFAULT_LANG` for ``None`` or an unrecognized value, so a stray language
    can never break a cart render or an order snapshot.

    Args:
        lang: Raw language input (query value, ``None``, or anything else).

    Returns:
        str: A validated language code from :data:`ALLOWED_LANGS`.
    """
    if lang is None:
        return DEFAULT_LANG
    normalized = lang.strip().lower()
    if normalized in ALLOWED_LANGS:
        return normalized
    return DEFAULT_LANG
