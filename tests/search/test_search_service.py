"""Direct service tests for search + facets business logic (B3).

Exercise :class:`~app.services.search_service.SearchService` without HTTP to
reach the branches the router/API tests do not surface under async coverage:
the blank-query short-circuit, the ranked-page hydration loop (including the
"ranked product with no card row is skipped" defensive branch), facet assembly,
the unknown-category error, and the ``_group_facets`` grouping (second value of
an already-seen attribute).

FTS-visible products are seeded via ``product_translation`` so the DB trigger
fills ``search_vector`` inside the test transaction.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.search_service import (
    DEFAULT_SEARCH_PAGE_SIZE,
    NotFoundError,
    SearchService,
)

# Category the search-domain seed tree places products under (see conftest).
CHILD_CATEGORY_ID: int = 2
# Language whose tsearch config (``russian``) the seeded names are written for.
SEARCH_LANG: str = "ru"
# Root-category localized slug used to address facets (from the seed tree).
ROOT_SLUG_RU: str = "electronika"


class TestSearchServiceSearch:
    """Service-level ``search`` pipeline: blank query, hydration, skip branch."""

    @pytest.mark.asyncio
    async def test_search_blank_query_returns_empty_page(self, seed):
        """A whitespace-only query short-circuits to an empty, well-formed page
        without hitting the repository (the ``not normalized`` guard)."""
        # Arrange: tree only; no repo call should be needed.
        await seed["build_tree"]()
        service = SearchService(seed["session"])

        # Act: query is blank after strip().
        result = await service.search(
            query="   ", lang=SEARCH_LANG, page=3, page_size=7
        )

        # Assert: empty data but the clamped page/size are echoed back.
        assert result.total == 0, "blank query yields zero total"
        assert result.data == [], "blank query yields no hits"
        assert result.page == 3, "the requested page is preserved"
        assert result.page_size == 7, "the requested page size is preserved"

    @pytest.mark.asyncio
    async def test_search_hydrates_ranked_hits_with_rank(self, seed):
        """A matching query returns hydrated cards paired with their rank,
        covering the rank → slice → load_cards → hits assembly path."""
        # Arrange: one active product with a rebuilt card so it hydrates.
        await seed["build_tree"]()
        session = seed["session"]
        await seed["add_product"](
            session,
            product_id=400,
            category_id=CHILD_CATEGORY_ID,
            code="SVC-1",
            price=Decimal("321.00"),
            qty=2,
            slugs={"ru": "svc-ru", "ro": "svc-ro"},
            names={"ru": "Наушники", "ro": "Casti"},
        )
        await session.flush()
        await seed["service"].rebuild_card(400)
        service = SearchService(session)

        # Act.
        result = await service.search(query="наушники", lang=SEARCH_LANG)

        # Assert: one hydrated hit with a positive rank and the default size.
        assert result.total == 1, "the single match is counted"
        assert len(result.data) == 1, "the single match is hydrated"
        assert result.data[0].card.product_id == 400, "the correct card is returned"
        assert result.data[0].rank > 0, "a matched hit carries a positive rank"
        assert result.page_size == DEFAULT_SEARCH_PAGE_SIZE, "default size applies"

    @pytest.mark.asyncio
    async def test_search_skips_ranked_product_without_card(self, seed):
        """A product that ranks (FTS hit) but has no ``product_card`` row for the
        language is skipped, not surfaced (the ``card is None: continue`` guard)."""
        # Arrange: active product (so FTS finds it) but NO rebuilt card row.
        await seed["build_tree"]()
        session = seed["session"]
        await seed["add_product"](
            session,
            product_id=401,
            category_id=CHILD_CATEGORY_ID,
            code="NOCARD-1",
            price=Decimal("15.00"),
            qty=1,
            slugs={"ru": "nocard-ru", "ro": "nocard-ro"},
            names={"ru": "Колонка", "ro": "Boxa"},
        )
        await session.flush()
        # Deliberately do NOT call rebuild_card -> no card exists for this id.
        service = SearchService(session)

        # Act.
        result = await service.search(query="колонка", lang=SEARCH_LANG)

        # Assert: it counts in total (ranked) but is dropped from hydrated data.
        assert result.total == 1, "the ranked product still counts toward total"
        assert result.data == [], "a ranked product without a card is not surfaced"

    @pytest.mark.asyncio
    async def test_search_second_page_slices_ranked_ids(self, seed):
        """Page 2 with size 1 returns the second-ranked product only, proving the
        ``start:start+size`` slice over the ranked list is applied."""
        # Arrange: two matching active products with rebuilt cards.
        await seed["build_tree"]()
        session = seed["session"]
        for product_id, code in ((410, "PG-A"), (411, "PG-B")):
            await seed["add_product"](
                session,
                product_id=product_id,
                category_id=CHILD_CATEGORY_ID,
                code=code,
                price=Decimal("200.00"),
                qty=1,
                slugs={"ru": f"{code}-ru", "ro": f"{code}-ro"},
                names={"ru": "Монитор", "ro": "Monitor"},
            )
        await session.flush()
        await seed["service"].rebuild_cards([410, 411])
        service = SearchService(session)

        # Act: page 2, size 1.
        result = await service.search(
            query="монитор", lang=SEARCH_LANG, page=2, page_size=1
        )

        # Assert: total counts both, but only one hit is on the second page.
        assert result.total == 2, "both matches are counted in total"
        assert len(result.data) == 1, "page size 1 yields a single hit"
        assert result.page == 2, "the second page is reported"


class TestSearchServiceFacets:
    """Service-level ``facets``: unknown slug error, happy-path assembly."""

    @pytest.mark.asyncio
    async def test_facets_unknown_slug_raises_not_found(self, seed):
        """An unresolvable category slug raises :class:`NotFoundError` before any
        aggregate query runs (covers the ``category_id is None`` branch)."""
        # Arrange: tree exists but the slug does not.
        await seed["build_tree"]()
        service = SearchService(seed["session"])

        # Act / Assert: the domain error is raised with the slug in its message.
        with pytest.raises(NotFoundError, match="no-such-slug"):
            await service.facets(category_slug="no-such-slug", lang=SEARCH_LANG)

    @pytest.mark.asyncio
    async def test_facets_assembles_attributes_and_price_bounds(self, seed):
        """A known slug yields grouped attribute facets plus price bounds,
        covering the assembly of :class:`FacetsResponse` from the repo rows."""
        # Arrange: reuse the shared facet seed (2 active phones + 1 inactive).
        await seed["build_tree"]()
        await _seed_facet_products(seed)
        service = SearchService(seed["session"])

        # Act: facet the root slug (subtree via path reaches the child products).
        response = await service.facets(category_slug=ROOT_SLUG_RU, lang=SEARCH_LANG)

        # Assert: attributes present and price bounds ignore the inactive product.
        codes = {attr.code for attr in response.attributes}
        assert codes == {"ram", "brand"}, "both active attributes are faceted"
        assert response.price_min == Decimal("4999.00"), "min ignores inactive card"
        assert response.price_max == Decimal("9999.00"), "max ignores inactive card"


class TestSearchServiceGrouping:
    """Unit tests for the pure ``_group_facets`` row-grouping helper."""

    def test_group_facets_appends_second_value_to_existing_attribute(self):
        """Two rows sharing an ``attribute_id`` collapse into ONE attribute with
        both values (the ``attribute is None`` check is False on the 2nd row)."""
        # Arrange: two value rows for the same attribute (id=1), one for another.
        rows = [
            SimpleNamespace(
                attribute_id=1,
                code="ram",
                attr_name="RAM",
                value_id=10,
                value="8 GB",
                count=2,
            ),
            SimpleNamespace(
                attribute_id=1,
                code="ram",
                attr_name="RAM",
                value_id=11,
                value="16 GB",
                count=5,
            ),
            SimpleNamespace(
                attribute_id=2,
                code="brand",
                attr_name="Brand",
                value_id=20,
                value="Acme",
                count=3,
            ),
        ]

        # Act.
        grouped = SearchService._group_facets(rows)

        # Assert: one entry per attribute; the shared one holds both values.
        by_code = {attr.code: attr for attr in grouped}
        assert set(by_code) == {"ram", "brand"}, "one attribute entry per id"
        ram_values = {v.value: v.count for v in by_code["ram"].values}
        assert ram_values == {"8 GB": 2, "16 GB": 5}, (
            "the second value must append to the existing attribute, not a new one"
        )
        assert len(by_code["ram"].values) == 2, "both ram values are grouped together"


async def _seed_facet_products(seed) -> None:
    """Two active phones (ram 8 + Samsung) + one inactive, mirroring the API
    facet fixture so service assembly runs over realistic aggregate rows."""
    session = seed["session"]
    service = seed["service"]
    models = seed["models"]

    session.add(models["Attribute"](id=1, code="ram"))
    session.add(
        models["AttributeTranslation"](attribute_id=1, lang="ru", name="Память")
    )
    session.add(models["Attribute"](id=2, code="brand"))
    session.add(models["AttributeTranslation"](attribute_id=2, lang="ru", name="Бренд"))
    session.add(models["AttributeValue"](id=1, attribute_id=1))
    session.add(
        models["AttributeValueTranslation"](value_id=1, lang="ru", value="8 ГБ")
    )
    session.add(models["AttributeValue"](id=2, attribute_id=1))
    session.add(
        models["AttributeValueTranslation"](value_id=2, lang="ru", value="16 ГБ")
    )
    session.add(models["AttributeValue"](id=3, attribute_id=2))
    session.add(
        models["AttributeValueTranslation"](value_id=3, lang="ru", value="Samsung")
    )
    await session.flush()

    await seed["add_product"](
        session,
        product_id=500,
        category_id=CHILD_CATEGORY_ID,
        code="FS-A",
        price=Decimal("4999.00"),
        qty=2,
        slugs={"ru": "fsa-ru", "ro": "fsa-ro"},
        names={"ru": "Товар А", "ro": "Produs A"},
    )
    await seed["add_product"](
        session,
        product_id=501,
        category_id=CHILD_CATEGORY_ID,
        code="FS-B",
        price=Decimal("9999.00"),
        qty=1,
        slugs={"ru": "fsb-ru", "ro": "fsb-ro"},
        names={"ru": "Товар Б", "ro": "Produs B"},
    )
    await seed["add_product"](
        session,
        product_id=502,
        category_id=CHILD_CATEGORY_ID,
        code="FS-C",
        price=Decimal("99999.00"),
        qty=0,
        slugs={"ru": "fsc-ru", "ro": "fsc-ro"},
        names={"ru": "Товар В", "ro": "Produs C"},
        is_active=False,
    )
    await session.flush()

    session.add(models["ProductAttribute"](product_id=500, value_id=1))
    session.add(models["ProductAttribute"](product_id=500, value_id=3))
    session.add(models["ProductAttribute"](product_id=501, value_id=1))
    session.add(models["ProductAttribute"](product_id=501, value_id=3))
    session.add(models["ProductAttribute"](product_id=502, value_id=2))
    await session.flush()
    await service.rebuild_cards([500, 501, 502])
