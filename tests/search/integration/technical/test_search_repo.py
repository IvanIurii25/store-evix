"""Direct repository tests for the search + facets data-access layer (B3).

These exercise :class:`~app.repositories.search_repo.SearchRepository` methods
without HTTP, targeting the merge/hydration/aggregate branches the API tests do
not surface under async coverage: the two-signal rank merge in
``search_product_ids`` (FTS row + article-code hit for the *same* product),
``load_cards`` returning a populated map, and ``price_bounds`` unpacking the
aggregate row for a populated subtree.

Products are seeded via ``product_translation`` so the DB trigger fills
``search_vector`` inside the test transaction (real ``ts_rank`` / FTS path).
"""

from decimal import Decimal

import pytest

from app.repositories.search_repo import CODE_MATCH_RANK, SearchRepository

# Category the search-domain seed tree places products under (see conftest).
CHILD_CATEGORY_ID: int = 2
# Language whose tsearch config (``russian``) the seeded names are written for.
SEARCH_LANG: str = "ru"


@pytest.mark.asyncio
async def test_search_product_ids_merges_fts_and_code_for_same_product(seed):
    """A product matched by BOTH its FTS name and its article code keeps the
    higher (code-boost) rank after the merge, proving the merge loop runs."""
    # Arrange: one product whose article code is a real Russian word that also
    # appears (stemmed) in its localized name — so FTS and code both hit it.
    await seed["build_tree"]()
    session = seed["session"]
    await seed["add_product"](
        session,
        product_id=300,
        category_id=CHILD_CATEGORY_ID,
        code="телефон",
        price=Decimal("100.00"),
        qty=1,
        slugs={"ru": "dual-ru", "ro": "dual-ro"},
        names={"ru": "Телефон", "ro": "Telefon"},
    )
    await session.flush()
    repo = SearchRepository(session)

    # Act: query the shared term — it matches the name via FTS and the code.
    ranked = await repo.search_product_ids("телефон", SEARCH_LANG)

    # Assert: the product appears once, boosted to the code-match rank floor.
    by_id = dict(ranked)
    assert 300 in by_id, "product matched by name+code must be present"
    assert by_id[300] == CODE_MATCH_RANK, (
        "code-match boost must win over the raw ts_rank for the merged product"
    )
    assert len(ranked) == 1, "the merge must de-duplicate the single product"


@pytest.mark.asyncio
async def test_search_product_ids_code_only_match_uses_boost_rank(seed):
    """A product matched only by article code (name unrelated) still ranks at
    the code-boost floor — covering the code-id merge branch in isolation."""
    # Arrange: name has no relation to the code token.
    await seed["build_tree"]()
    session = seed["session"]
    await seed["add_product"](
        session,
        product_id=301,
        category_id=CHILD_CATEGORY_ID,
        code="SKU-ABC",
        price=Decimal("50.00"),
        qty=1,
        slugs={"ru": "misc-ru", "ro": "misc-ro"},
        names={"ru": "Кастрюля", "ro": "Cratita"},
    )
    await session.flush()
    repo = SearchRepository(session)

    # Act: query equals the article code (case-insensitive ilike).
    ranked = await repo.search_product_ids("sku-abc", SEARCH_LANG)

    # Assert: matched purely by code, at the boost rank.
    assert ranked == [(301, CODE_MATCH_RANK)], (
        "code-only hit must yield exactly the boosted product"
    )


@pytest.mark.asyncio
async def test_load_cards_returns_map_for_existing_ids(seed):
    """``load_cards`` hydrates a non-empty id list into a ``product_id -> card``
    map for cards active in the language (covers the SELECT + dict build)."""
    # Arrange: an active product with a rebuilt card row for the language.
    await seed["build_tree"]()
    session = seed["session"]
    service = seed["service"]
    await seed["add_product"](
        session,
        product_id=310,
        category_id=CHILD_CATEGORY_ID,
        code="CARD-1",
        price=Decimal("777.00"),
        qty=4,
        slugs={"ru": "card-ru", "ro": "card-ro"},
        names={"ru": "Планшет", "ro": "Tableta"},
    )
    await session.flush()
    await service.rebuild_card(310)
    repo = SearchRepository(session)

    # Act: hydrate the id (plus one id that has no card, to exercise the filter).
    cards = await repo.load_cards([310, 999_999], SEARCH_LANG)

    # Assert: only the existing active card is returned, keyed by product_id.
    assert set(cards) == {310}, "only ids with an active card in lang are present"
    assert cards[310].product_id == 310, "the card must be keyed by its product_id"


@pytest.mark.asyncio
async def test_load_cards_empty_ids_short_circuits(seed):
    """An empty id list returns ``{}`` without touching the DB (early return)."""
    # Arrange: no data needed for the guard clause.
    session = seed["session"]
    repo = SearchRepository(session)

    # Act.
    cards = await repo.load_cards([], SEARCH_LANG)

    # Assert: empty map, no query issued.
    assert cards == {}, "empty id list must short-circuit to an empty map"


@pytest.mark.asyncio
async def test_price_bounds_returns_min_max_for_populated_subtree(seed):
    """``price_bounds`` unpacks the aggregate row into ``(min, max)`` for a
    subtree that has active cards (covers the row-unpacking return line)."""
    # Arrange: two active products at distinct prices under the child category.
    await seed["build_tree"]()
    session = seed["session"]
    service = seed["service"]
    for product_id, price, code in ((320, "10.00", "P-LO"), (321, "90.00", "P-HI")):
        await seed["add_product"](
            session,
            product_id=product_id,
            category_id=CHILD_CATEGORY_ID,
            code=code,
            price=Decimal(price),
            qty=1,
            slugs={"ru": f"{code}-ru", "ro": f"{code}-ro"},
            names={"ru": "Часы", "ro": "Ceas"},
        )
    await session.flush()
    await service.rebuild_cards([320, 321])
    repo = SearchRepository(session)

    # Act: bounds over the child subtree in the language.
    price_min, price_max = await repo.price_bounds(CHILD_CATEGORY_ID, SEARCH_LANG)

    # Assert: min/max reflect the two seeded prices.
    assert price_min == Decimal("10.00"), "min price must be the cheapest card"
    assert price_max == Decimal("90.00"), "max price must be the dearest card"


@pytest.mark.asyncio
async def test_price_bounds_empty_subtree_returns_none_pair(seed):
    """A subtree with no active cards yields ``(None, None)`` from the aggregate
    (the aggregate still returns exactly one row, unpacked to two ``None`` s)."""
    # Arrange: tree only, no products under it.
    await seed["build_tree"]()
    session = seed["session"]
    repo = SearchRepository(session)

    # Act.
    price_min, price_max = await repo.price_bounds(CHILD_CATEGORY_ID, SEARCH_LANG)

    # Assert: both bounds are None for the empty subtree.
    assert price_min is None, "empty subtree has no minimum price"
    assert price_max is None, "empty subtree has no maximum price"
