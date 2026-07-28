"""Router-level tests for the search endpoints (B3).

The ASGI ``client`` runs handlers in a separate anyio task the line tracer does
not follow, so the unknown-slug path of ``category_facets`` is covered here by
awaiting the handler coroutine directly with a real session (the same dependency
the router receives), asserting it propagates
:class:`~app.services.search_service.NotFoundError` (a
:class:`~app.core.errors.DomainError` the unified handler renders as 404).
"""

import pytest
from fastapi import status

from app.api.routers.search import category_facets, search
from app.services.search_service import DEFAULT_SEARCH_PAGE_SIZE, NotFoundError

# Language whose tsearch config the seed tree translations are written for.
SEARCH_LANG: str = "ru"


class TestCategoryFacetsRouter:
    """Direct coroutine calls into the facets route handler."""

    @pytest.mark.asyncio
    async def test_unknown_slug_raises_not_found(self, seed):
        """An unknown category slug makes the handler propagate ``NotFoundError``
        (a ``DomainError`` carrying status 404 that the unified handler renders)."""
        # Arrange: tree exists but the requested slug is absent.
        await seed["build_tree"]()
        session = seed["session"]

        # Act / Assert: the handler surfaces the domain error unchanged.
        with pytest.raises(NotFoundError) as excinfo:
            await category_facets(
                slug="definitely-missing", lang=SEARCH_LANG, session=session
            )
        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND, (
            "an unknown slug must be surfaced as HTTP 404"
        )
        assert "definitely-missing" in str(excinfo.value), (
            "the error message must carry the offending slug"
        )


class TestSearchRouter:
    """Direct coroutine call into the search route handler (happy path)."""

    @pytest.mark.asyncio
    async def test_search_handler_returns_ranked_response(self, seed):
        """The search handler delegates to the service and returns the ranked
        :class:`SearchResponse` for a matching query."""
        # Arrange: one active product with a rebuilt card so it hydrates.
        from decimal import Decimal

        await seed["build_tree"]()
        session = seed["session"]
        await seed["add_product"](
            session,
            product_id=600,
            category_id=2,
            code="RT-1",
            price=Decimal("42.00"),
            qty=1,
            slugs={"ru": "rt-ru", "ro": "rt-ro"},
            names={"ru": "Клавиатура", "ro": "Tastatura"},
        )
        await session.flush()
        await seed["service"].rebuild_card(600)

        # Act: call the handler coroutine directly with resolved dependencies.
        response = await search(
            q="клавиатура",
            page=1,
            page_size=DEFAULT_SEARCH_PAGE_SIZE,
            lang=SEARCH_LANG,
            session=session,
        )

        # Assert: the ranked response carries the single hydrated hit.
        assert response.total == 1, "the single match is counted"
        assert response.data[0].card.product_id == 600, "the correct card is returned"
