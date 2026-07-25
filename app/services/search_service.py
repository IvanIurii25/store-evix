"""Search + facets business logic (stage B3).

Holds the rules the router and repository must not: query normalization, page
pagination over ranked results, hydration of matched products from the
``product_card`` read-model (§5.2), and assembly of the live facets response
(§6.2). SQL lives in :class:`~app.repositories.search_repo.SearchRepository`;
this service knows nothing about HTTP and raises domain errors the router maps.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.search_repo import SearchRepository
from app.schemas.catalog import ProductCardOut
from app.schemas.search import (
    FacetAttribute,
    FacetsResponse,
    FacetValue,
    SearchHit,
    SearchResponse,
)

logger = logging.getLogger(__name__)

# Page size for search results (classic page pagination — §4).
DEFAULT_SEARCH_PAGE_SIZE: int = 24
MAX_SEARCH_PAGE_SIZE: int = 100


class SearchError(Exception):
    """Base class for search domain errors (mapped to HTTP by the router)."""


class NotFoundError(SearchError):
    """A referenced resource (e.g. a category slug) does not exist / is hidden."""


class SearchService:
    """Full-text search + live facet operations for the storefront."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session used for all read queries.
        """
        self.session = session
        self.repo = SearchRepository(session)

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        lang: str,
        page: int = 1,
        page_size: int = DEFAULT_SEARCH_PAGE_SIZE,
    ) -> SearchResponse:
        """Run a ranked full-text search and return one page of listing cards.

        Pipeline: rank all matching ``product_id`` s (FTS over
        ``product_translation`` for ``lang`` + article-code match), slice the
        requested page in relevance order, then hydrate those ids from
        ``product_card`` in a single query (no N+1). Empty/blank queries return
        an empty page rather than scanning the catalog.

        Args:
            query: Raw user query string.
            lang: Request language code (selects the tsearch config).
            page: 1-based page number.
            page_size: Requested page size (clamped to ``MAX_SEARCH_PAGE_SIZE``).

        Returns:
            SearchResponse: ``{data, total, page, page_size}`` ordered by rank.
        """
        normalized = query.strip()
        safe_page = max(1, page)
        safe_size = max(1, min(page_size, MAX_SEARCH_PAGE_SIZE))
        if not normalized:
            return SearchResponse(data=[], total=0, page=safe_page, page_size=safe_size)

        # Elasticsearch backend when selected AND reachable; a failed ES round-trip
        # (down, timeout, empty index error) transparently falls back to Postgres
        # FTS so search never goes dark on an ES incident (plan §3/§8).
        if settings.search_uses_elastic:
            try:
                return await self._search_elastic(
                    normalized, lang, safe_page, safe_size
                )
            except Exception as exc:  # noqa: BLE001 — any ES failure → fallback
                logger.warning(
                    "es_search_failed query=%r err=%s — falling back to postgres",
                    normalized,
                    exc,
                )

        return await self._search_postgres(normalized, lang, safe_page, safe_size)

    async def _search_elastic(
        self, query: str, lang: str, page: int, page_size: int
    ) -> SearchResponse:
        """ES-backed page: rank ids in ES, hydrate cards from ``product_card``.

        ES does its own ``from/size`` pagination and returns the page's ranked
        ids + the total; hydration reuses the same ``load_cards`` path as the
        Postgres branch, so the card contract is identical.
        """
        from app.search.es.backend import EsSearchBackend

        result = await EsSearchBackend().search(
            query, page=page, page_size=page_size
        )
        page_ids = [pid for pid, _ in result.hits]
        cards = await self.repo.load_cards(page_ids, lang)
        hits: list[SearchHit] = []
        for pid, score in result.hits:
            card = cards.get(pid)
            if card is None:
                # Ranked in ES but no card row in this lang (inactive / not
                # published) — skip rather than surface a half-hydrated hit.
                continue
            hits.append(SearchHit(card=ProductCardOut.model_validate(card), rank=score))

        return SearchResponse(
            data=hits,
            total=result.total,
            page=page,
            page_size=page_size,
            suggestions=result.suggestions,
        )

    async def _search_postgres(
        self, query: str, lang: str, page: int, page_size: int
    ) -> SearchResponse:
        """Native Postgres FTS page (the original path; also the ES fallback)."""
        ranked = await self.repo.search_product_ids(query, lang)
        total = len(ranked)

        start = (page - 1) * page_size
        page_slice = ranked[start : start + page_size]
        page_ids = [product_id for product_id, _ in page_slice]

        cards = await self.repo.load_cards(page_ids, lang)
        hits: list[SearchHit] = []
        for product_id, rank in page_slice:
            card = cards.get(product_id)
            if card is None:
                # A ranked product without a card row for this lang (should not
                # happen under the publication rule) is skipped, not surfaced.
                continue
            hits.append(SearchHit(card=ProductCardOut.model_validate(card), rank=rank))

        return SearchResponse(
            data=hits, total=total, page=page, page_size=page_size
        )

    # ------------------------------------------------------------------ #
    # Facets (§6.2)
    # ------------------------------------------------------------------ #
    async def facets(self, category_slug: str, lang: str) -> FacetsResponse:
        """Compute live facets for a category subtree by localized slug (§6.2).

        Resolves the slug to an active category, then computes attribute-value
        counts and price bounds over the active ``product_card`` subtree with two
        live queries (no Redis index — bulk imports cannot drift counts).

        Args:
            category_slug: Localized slug of the category to facet.
            lang: Request language code.

        Returns:
            FacetsResponse: Attribute facets + price bounds for the subtree.

        Raises:
            NotFoundError: If no active category has that slug in the language.
        """
        category_id = await self.repo.get_category_id_by_slug(category_slug, lang)
        if category_id is None:
            raise NotFoundError(f"Category '{category_slug}' not found")

        value_rows = await self.repo.facet_values(category_id, lang)
        price_min, price_max = await self.repo.price_bounds(category_id, lang)
        attributes = self._group_facets(value_rows)

        return FacetsResponse(
            attributes=attributes,
            price_min=price_min,
            price_max=price_max,
        )

    @staticmethod
    def _group_facets(rows) -> list[FacetAttribute]:
        """Group flat ``(attribute, value, count)`` rows per attribute (§6.2).

        Args:
            rows: Rows of ``(attribute_id, code, attr_name, value_id, value,
                count)`` ordered by attribute code then value.

        Returns:
            list[FacetAttribute]: One entry per attribute with its values+counts.
        """
        grouped: dict[int, FacetAttribute] = {}
        for row in rows:
            attribute = grouped.get(row.attribute_id)
            if attribute is None:
                attribute = FacetAttribute(
                    attribute_id=row.attribute_id,
                    code=row.code,
                    name=row.attr_name,
                    values=[],
                )
                grouped[row.attribute_id] = attribute
            attribute.values.append(
                FacetValue(
                    value_id=row.value_id,
                    value=row.value,
                    count=int(row.count),
                )
            )
        return list(grouped.values())
