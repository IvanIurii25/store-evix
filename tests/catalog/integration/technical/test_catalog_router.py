"""Router-level error-mapping tests for the catalog listing endpoints.

The service-level listing tests (``test_listing.py``) raise ``NotFoundError`` /
``InvalidCursorError`` directly, but never drive them through the *router*, so
the router's ``except`` → HTTPException mappings stay uncovered. These tests hit
the ASGI app (domain ``client`` fixture) to exercise:

* ``GET /catalog/categories/{slug}/products`` — unknown category → 404
  (NotFoundError) and a malformed cursor → 400 (InvalidCursorError);
* ``GET /catalog/products`` (store-wide) — a malformed cursor → 400
  (InvalidCursorError).
"""

import pytest

pytestmark = pytest.mark.asyncio

# A cursor value that fails base64/opaque decoding in the service.
_BAD_CURSOR: str = "not-base64!!"
# Expected HTTP statuses.
_HTTP_NOT_FOUND: int = 404
_HTTP_BAD_REQUEST: int = 400
# Unified error envelope codes carried by the migrated domain exceptions.
_NOT_FOUND_CODE: str = "not_found"
_INVALID_CURSOR_CODE: str = "invalid_cursor"


class TestCategoryProductsErrors:
    """``/categories/{slug}/products`` error mappings (lines 130-135)."""

    async def test_unknown_category_returns_404(self, seed, client) -> None:
        """An unknown category slug maps NotFoundError → 404 envelope."""
        # Arrange: a valid tree exists, but we query a slug that is not in it.
        await seed["build_tree"]()

        # Act: list products under a non-existent category slug.
        resp = await client.get("/api/v1/catalog/categories/missing/products?lang=ro")

        # Assert: 404 in the unified error envelope.
        assert resp.status_code == _HTTP_NOT_FOUND, "unknown category → 404"
        assert resp.json()["error"]["code"] == _NOT_FOUND_CODE, "unified envelope"

    async def test_bad_cursor_returns_400(self, seed, client) -> None:
        """A malformed cursor maps InvalidCursorError → 400 envelope."""
        # Arrange: a real category (telefoane) so the slug resolves; only the
        # cursor is invalid, isolating the InvalidCursorError branch from 404.
        await seed["build_tree"]()

        # Act: list products for a valid category but with a garbage cursor.
        resp = await client.get(
            "/api/v1/catalog/categories/telefoane/products",
            params={"lang": "ro", "cursor": _BAD_CURSOR},
        )

        # Assert: 400 in the unified error envelope.
        assert resp.status_code == _HTTP_BAD_REQUEST, "bad cursor → 400"
        assert resp.json()["error"]["code"] == _INVALID_CURSOR_CODE, "unified envelope"


class TestAllProductsErrors:
    """Store-wide ``/products`` error mapping (lines 184-185)."""

    async def test_bad_cursor_returns_400(self, seed, client) -> None:
        """A malformed store-wide cursor maps InvalidCursorError → 400 envelope."""
        # Arrange: a tree exists; the store-wide listing needs no category slug.
        await seed["build_tree"]()

        # Act: request the store-wide listing with a garbage cursor.
        resp = await client.get(
            "/api/v1/catalog/products",
            params={"lang": "ro", "cursor": _BAD_CURSOR},
        )

        # Assert: 400 in the unified error envelope.
        assert resp.status_code == _HTTP_BAD_REQUEST, "bad cursor → 400"
        assert resp.json()["error"]["code"] == _INVALID_CURSOR_CODE, "unified envelope"
