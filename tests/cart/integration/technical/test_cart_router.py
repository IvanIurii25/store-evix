"""Router-level tests for the cart endpoints (fills the router branch gaps).

Complements ``test_cart_api.py`` (guest add / merge-sum / live totals) by
driving the *router* branches its peers leave uncovered:

* the malformed ``session_token`` cookie path (``_read_session_token`` returns
  ``None`` for a non-UUID cookie);
* ``PATCH /cart/items/{id}`` — the happy quantity-set path and its
  ``ItemNotFoundError`` → 404 mapping;
* ``DELETE /cart/items/{id}`` — the happy remove path and its
  ``ItemNotFoundError`` → 404 mapping;
* ``POST /cart/merge`` with no guest cookie — the "return the plain user cart"
  short-circuit (no guest cart to fold in).

All tests hit the ASGI app via the domain ``client`` / ``user_client`` fixtures
(``tests/cart/conftest.py``); only the DB session is overridden.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

# Product id + price reused across cart router scenarios.
_PRODUCT_ID: int = 1
_PRODUCT_PRICE: Decimal = Decimal("10.00")
_PRODUCT_CODE: str = "P1"
# A product id that is never added to any cart — drives the not-found branch.
_ABSENT_PRODUCT_ID: int = 999
# Expected HTTP statuses.
_HTTP_OK: int = 200
_HTTP_CREATED: int = 201
_HTTP_NOT_FOUND: int = 404
# Unified error envelope code for the item-not-found domain error.
_ERROR_CODE: str = "item_not_found"


async def _guest_add(client: AsyncClient, qty: int) -> AsyncClient:
    """Add ``_PRODUCT_ID`` as a guest and pin the issued cookie on the client."""
    resp = await client.post(
        "/api/v1/cart/items", json={"product_id": _PRODUCT_ID, "qty": qty}
    )
    # The guest cookie must be minted so subsequent calls hit the same cart.
    client.cookies.set("session_token", resp.cookies["session_token"])
    return client


class TestCartCookieHandling:
    """The guest ``session_token`` cookie parse branch (lines 56-57)."""

    async def test_add_item_malformed_cookie_mints_fresh_token(
        self,
        client: AsyncClient,
        add_product,
    ) -> None:
        """A non-UUID cookie is ignored and a fresh valid token is issued."""
        # Arrange: an active product + a garbage (non-UUID) session cookie.
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)
        client.cookies.set("session_token", "not-a-uuid")

        # Act: add as a guest; the malformed cookie must not blow up parsing.
        resp = await client.post(
            "/api/v1/cart/items", json={"product_id": _PRODUCT_ID, "qty": 1}
        )

        # Assert: the item lands and a *valid* replacement cookie is set.
        assert resp.status_code == _HTTP_CREATED, "add must succeed"
        assert resp.json()["item_count"] == 1, "one unit added"
        assert "session_token" in resp.cookies, "a fresh token cookie is issued"
        assert resp.cookies["session_token"] != "not-a-uuid", (
            "the garbage cookie is replaced by a real UUID token"
        )


class TestCartItemMutations:
    """``PATCH``/``DELETE`` on ``/cart/items/{id}`` (lines 171-179, 207-213)."""

    async def test_update_item_sets_absolute_quantity(
        self,
        client: AsyncClient,
        add_product,
    ) -> None:
        """PATCH sets the absolute line quantity and re-renders the cart."""
        # Arrange: a guest cart holding 2 units of the product.
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)
        await _guest_add(client, qty=2)

        # Act: overwrite the quantity to 5 (absolute, not delta).
        resp = await client.patch(f"/api/v1/cart/items/{_PRODUCT_ID}", json={"qty": 5})

        # Assert: the line now carries exactly 5 units.
        assert resp.status_code == _HTTP_OK, "patch on an existing line succeeds"
        assert resp.json()["item_count"] == 5, "quantity is set absolutely"

    async def test_update_item_missing_line_returns_404(
        self,
        client: AsyncClient,
        add_product,
    ) -> None:
        """PATCH on a product not in the cart maps ItemNotFoundError → 404."""
        # Arrange: a guest cart with one product, plus a second unrelated one.
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)
        await add_product(_ABSENT_PRODUCT_ID, price=_PRODUCT_PRICE, code="P999")
        await _guest_add(client, qty=1)

        # Act: patch the product that was never added to this cart.
        resp = await client.patch(
            f"/api/v1/cart/items/{_ABSENT_PRODUCT_ID}", json={"qty": 3}
        )

        # Assert: 404 in the unified error envelope.
        assert resp.status_code == _HTTP_NOT_FOUND, "missing line → 404"
        assert resp.json()["error"]["code"] == _ERROR_CODE, "unified envelope"

    async def test_remove_item_drops_line(
        self,
        client: AsyncClient,
        add_product,
    ) -> None:
        """DELETE removes the line and returns the emptied cart."""
        # Arrange: a guest cart holding one product line.
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)
        await _guest_add(client, qty=1)

        # Act: delete that line.
        resp = await client.delete(f"/api/v1/cart/items/{_PRODUCT_ID}")

        # Assert: the cart is now empty.
        assert resp.status_code == _HTTP_OK, "delete on an existing line succeeds"
        assert resp.json()["item_count"] == 0, "the line is gone"

    async def test_remove_item_missing_line_returns_404(
        self,
        client: AsyncClient,
        add_product,
    ) -> None:
        """DELETE on a product not in the cart maps ItemNotFoundError → 404."""
        # Arrange: a guest cart with one product, plus a second unrelated one.
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)
        await add_product(_ABSENT_PRODUCT_ID, price=_PRODUCT_PRICE, code="P999")
        await _guest_add(client, qty=1)

        # Act: delete the product that was never added to this cart.
        resp = await client.delete(f"/api/v1/cart/items/{_ABSENT_PRODUCT_ID}")

        # Assert: 404 in the unified error envelope.
        assert resp.status_code == _HTTP_NOT_FOUND, "missing line → 404"
        assert resp.json()["error"]["code"] == _ERROR_CODE, "unified envelope"


class TestCartMergeNoCookie:
    """``POST /cart/merge`` short-circuit when there is no guest cookie (line 244)."""

    async def test_merge_without_guest_cookie_returns_user_cart(
        self,
        user_client: AsyncClient,
        add_product,
    ) -> None:
        """No guest cookie → merge returns the plain user cart (no fold-in)."""
        # Arrange: an authenticated caller with no guest session_token cookie.
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)
        user_client.cookies.clear()

        # Act: merge with nothing to merge.
        resp = await user_client.post("/api/v1/cart/merge")

        # Assert: the empty user cart is returned as a plain 200.
        assert resp.status_code == _HTTP_OK, "merge is a no-op, still 200"
        assert resp.json()["item_count"] == 0, "no guest cart was folded in"


class TestCartAuthenticatedAdd:
    """The authenticated ``POST /cart/items`` path skips the guest-cookie block."""

    async def test_add_item_as_user_issues_no_cookie(
        self,
        user_client: AsyncClient,
        add_product,
    ) -> None:
        """An authenticated add uses the user cart and sets no guest cookie."""
        # Arrange: an authenticated caller (no guest cookie involved).
        await add_product(_PRODUCT_ID, price=_PRODUCT_PRICE, code=_PRODUCT_CODE)

        # Act: add as the logged-in user (guest-cookie branch is skipped).
        resp = await user_client.post(
            "/api/v1/cart/items", json={"product_id": _PRODUCT_ID, "qty": 2}
        )

        # Assert: the item lands on the user cart, no session_token is minted.
        assert resp.status_code == _HTTP_CREATED, "authenticated add succeeds"
        assert resp.json()["item_count"] == 2, "two units on the user cart"
        assert "session_token" not in resp.cookies, "no guest cookie for a user"
