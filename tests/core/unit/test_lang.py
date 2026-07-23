"""Unit tests for the language-resolution helpers in :mod:`app.api.lang`.

``get_lang`` and ``normalize_lang`` are pure resolvers, so each is called
directly with crafted query values and ``Accept-Language`` headers to cover the
explicit-override, header-fallback, default and invalid branches.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from app.api.lang import (
    DEFAULT_LANG,
    _parse_accept_language,
    get_lang,
    normalize_lang,
)
from tests.core._helpers import build_request

# A language outside ALLOWED_LANGS ("ru", "ro") — the invalid-input branch.
_UNSUPPORTED_LANG = "fr"


class TestLangGetLang:
    """``get_lang`` — explicit ``?lang=`` override vs header vs default."""

    def test_get_lang_explicit_supported_returns_normalized(self):
        # Arrange: an explicit, supported (upper-cased) override.
        request = build_request()

        # Act.
        resolved = get_lang(request, lang="RU")

        # Assert: an explicit value wins and is lower-cased.
        assert resolved == "ru", "explicit supported lang must be normalized + returned"

    def test_get_lang_explicit_unsupported_raises_400(self):
        # Arrange: an explicit but unsupported override.
        request = build_request()

        # Act / Assert: an unsupported explicit lang is rejected with 400.
        with pytest.raises(HTTPException) as exc_info:
            get_lang(request, lang=_UNSUPPORTED_LANG)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST, (
            "an unsupported explicit ?lang= must raise 400"
        )

    def test_get_lang_header_fallback_returns_header_lang(self):
        # Arrange: no override, a header whose first supported token is ``ro``.
        request = build_request(headers={"Accept-Language": "ro-RO,en;q=0.8"})

        # Act.
        resolved = get_lang(request, lang=None)

        # Assert: the header supplies the language when no override is given.
        assert resolved == "ro", "header must be the fallback when no ?lang= is set"

    def test_get_lang_no_override_no_header_returns_default(self):
        # Arrange: neither override nor header.
        request = build_request()

        # Act.
        resolved = get_lang(request, lang=None)

        # Assert: the module default is used as the last resort.
        assert resolved == DEFAULT_LANG, (
            "absent override + header must yield the default"
        )

    def test_get_lang_unsupported_header_returns_default(self):
        # Arrange: a header with only unsupported languages.
        request = build_request(headers={"Accept-Language": "fr-FR,en;q=0.5"})

        # Act.
        resolved = get_lang(request, lang=None)

        # Assert: an unmatched header falls through to the default.
        assert resolved == DEFAULT_LANG, "unsupported header must fall back to default"


class TestLangParseAcceptLanguage:
    """``_parse_accept_language`` header-token matching branches."""

    def test_parse_none_header_returns_none(self):
        # Arrange / Act: a missing header value.
        result = _parse_accept_language(None)

        # Assert: no header -> no match.
        assert result is None, "a None header must yield None"

    def test_parse_first_supported_token_returned(self):
        # Arrange: header where a supported token appears after an unsupported one.
        header = "de,fr;q=0.9,ru;q=0.8"

        # Act.
        result = _parse_accept_language(header)

        # Assert: the first supported base language is returned.
        assert result == "ru", "the first supported base token must be returned"

    def test_parse_no_supported_token_returns_none(self):
        # Arrange: a header with no supported languages.
        header = "de-DE,fr;q=0.9"

        # Act.
        result = _parse_accept_language(header)

        # Assert: no supported token -> ``None``.
        assert result is None, "a header with no supported token must yield None"


class TestLangNormalizeLang:
    """``normalize_lang`` — lenient, never-raising boundary coercion."""

    def test_normalize_none_returns_default(self):
        # Arrange / Act: a ``None`` input.
        result = normalize_lang(None)

        # Assert: ``None`` coerces to the default.
        assert result == DEFAULT_LANG, "None must coerce to the default lang"

    def test_normalize_supported_returns_normalized(self):
        # Arrange / Act: a supported value with surrounding noise.
        result = normalize_lang("  RO ")

        # Assert: a supported value is trimmed + lower-cased.
        assert result == "ro", "a supported value must be trimmed and lower-cased"

    def test_normalize_unsupported_returns_default(self):
        # Arrange / Act: an unsupported value.
        result = normalize_lang(_UNSUPPORTED_LANG)

        # Assert: an unrecognized value silently falls back to the default.
        assert result == DEFAULT_LANG, "an unsupported value must fall back to default"
