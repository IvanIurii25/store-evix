"""Related-products ES path: MLT ranking, buyable hydration, top-up, fallback.

The Postgres/same-category path is covered by ``test_related.py`` (default
backend). Here we drive the ``search_backend='elastic'`` branch with the ES
candidate fetch stubbed, so no live Elasticsearch is needed — the tests assert
the orchestration: ES relevance order is honoured, out-of-stock / self ids are
dropped, the rail is topped up when similar items are few, and any ES failure
falls back to the same-category newest set.
"""

from decimal import Decimal

import pytest

from app.core.config import settings
from app.search.es.related import build_related_body
from app.services.catalog_service import CatalogService


async def _seed_siblings(seed) -> None:
    """Category 2: current p101, in-stock p102/p103/p104, OOS p105; p201 elsewhere."""
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_category"](
        session,
        category_id=3,
        parent_id=None,
        path=[3],
        depth=0,
        position=1,
        slugs={"ru": "aksessuary", "ro": "accesorii"},
        names={"ru": "Аксессуары", "ro": "Accesorii"},
    )
    for index in range(1, 5):
        await seed["add_product"](
            session,
            product_id=100 + index,
            category_id=2,
            code=f"R-{index:03d}",
            price=Decimal(f"{index * 10}.00"),
            qty=index,  # all in stock (qty >= 1)
            slugs={"ru": f"r-{index}-ru", "ro": f"r-{index}-ro"},
            names={"ru": f"Товар {index}", "ro": f"Produs {index}"},
        )
    await seed["add_product"](
        session,
        product_id=105,
        category_id=2,
        code="R-OOS",
        price=Decimal("99.00"),
        qty=0,  # out of stock — never buyable cross-sell
        slugs={"ru": "r-oos-ru", "ro": "r-oos-ro"},
        names={"ru": "Нет в наличии", "ro": "Fără stoc"},
    )
    await seed["add_product"](
        session,
        product_id=201,
        category_id=3,
        code="R-OTHER",
        price=Decimal("15.00"),
        qty=5,
        slugs={"ru": "r-other-ru", "ro": "r-other-ro"},
        names={"ru": "Другая", "ro": "Alta"},
    )
    await session.flush()
    await service.rebuild_cards([101, 102, 103, 104, 105, 201])


def _use_elastic(monkeypatch, ids_or_exc) -> None:
    """Select the ES backend and stub the candidate fetch (ids list or exception)."""
    monkeypatch.setattr(settings, "search_backend", "elastic")

    async def fake_fetch(product_id: int, lang: str, size: int):
        if isinstance(ids_or_exc, Exception):
            raise ids_or_exc
        return list(ids_or_exc)

    monkeypatch.setattr("app.search.es.related.fetch_related_ids", fake_fetch)


# --------------------------------------------------------------------------- #
# Query builder (unit)
# --------------------------------------------------------------------------- #
def test_build_related_body_structure():
    """MLT body: boosted lang fields, relaxed floors, is_active filter, self excluded."""
    body = build_related_body(101, "ro", size=40)

    mlt = body["query"]["bool"]["must"][0]["more_like_this"]
    assert mlt["fields"] == ["name_ro^4", "attrs_ro^3", "category_ro^2", "desc_ro^1"]
    assert mlt["like"] == [{"_index": settings.elastic_index, "_id": "101"}]
    # Small-catalogue floors: defaults would return nothing.
    assert mlt["min_term_freq"] == 1
    assert mlt["min_doc_freq"] == 1
    assert mlt["include"] is False
    assert body["query"]["bool"]["filter"] == [{"term": {"is_active": True}}]
    assert body["query"]["bool"]["must_not"] == [{"ids": {"values": ["101"]}}]
    assert body["size"] == 40
    assert body["_source"] == ["id"]


def test_build_related_body_uses_request_language():
    """The ``_{lang}`` field variants follow the request language."""
    body = build_related_body(7, "ru", size=10)
    fields = body["query"]["bool"]["must"][0]["more_like_this"]["fields"]
    assert fields == ["name_ru^4", "attrs_ru^3", "category_ru^2", "desc_ru^1"]


@pytest.mark.asyncio
async def test_fetch_related_ids_parses_hits(monkeypatch):
    """fetch_related_ids returns ES hit ids in order, skipping hits without an id."""
    from app.search.es import related as related_mod

    class _FakeClient:
        async def search(self, index, body):  # noqa: ANN001 — test stub
            return {
                "hits": {
                    "hits": [
                        {"_source": {"id": 102}},
                        {"_source": {"id": 103}},
                        {"_source": {}},  # missing id → skipped
                    ]
                }
            }

    monkeypatch.setattr(related_mod, "get_es_client", lambda: _FakeClient())

    ids = await related_mod.fetch_related_ids(101, "ro", size=40)

    assert ids == [102, 103]


# --------------------------------------------------------------------------- #
# Service ES path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_related_es_preserves_relevance_order(seed, monkeypatch):
    """Cards come back in ES relevance order — not the Postgres newest-first order."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]
    _use_elastic(monkeypatch, [103, 102, 104])  # deliberately not newest-first

    cards = await service.related_products("r-1-ro", "ro", limit=8)

    assert [c.product_id for c in cards] == [103, 102, 104]


@pytest.mark.asyncio
async def test_related_es_respects_limit(seed, monkeypatch):
    """The limit caps the ES-ranked rail."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]
    _use_elastic(monkeypatch, [103, 102, 104])

    cards = await service.related_products("r-1-ro", "ro", limit=2)

    assert [c.product_id for c in cards] == [103, 102]


@pytest.mark.asyncio
async def test_related_es_drops_oos_and_self_then_tops_up(seed, monkeypatch):
    """OOS + self candidates are filtered; the short rail is topped up (newest)."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]
    # 105 is OOS, 101 is the seed itself → only 102 survives from ES; top-up adds
    # the same-category newest (104, 103) excluding what's already picked.
    _use_elastic(monkeypatch, [105, 101, 102])

    cards = await service.related_products("r-1-ro", "ro", limit=8)

    assert [c.product_id for c in cards] == [102, 104, 103]


@pytest.mark.asyncio
async def test_related_es_empty_falls_back_to_topup(seed, monkeypatch):
    """No ES candidates (e.g. seed not indexed) → full same-category newest rail."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]
    _use_elastic(monkeypatch, [])

    cards = await service.related_products("r-1-ro", "ro", limit=8)

    assert [c.product_id for c in cards] == [104, 103, 102]


@pytest.mark.asyncio
async def test_related_es_error_falls_back_to_postgres(seed, monkeypatch):
    """An ES round-trip failure falls back to the same-category newest set."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]
    _use_elastic(monkeypatch, RuntimeError("es down"))

    cards = await service.related_products("r-1-ro", "ro", limit=8)

    assert [c.product_id for c in cards] == [104, 103, 102]
