"""Async data-access layer for search + facets (stage B3).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.search_service.SearchService`). All queries are async
SQLAlchemy 2.0 ``select()`` statements.

Search (§6.1) is PostgreSQL full-text search over ``product_translation`` with a
per-language text-search config (``ru`` → ``russian``, ``ro`` → ``romanian``),
using ``websearch_to_tsquery`` + ``ts_rank``. The article ``product.code`` is
matched separately (case-insensitive), outside the FTS, since it is not
translated. Ranked ``product_id`` s are then hydrated from the denormalized
``product_card`` (§5.2) for the requested language.

Facets (§6.2) are computed by a live query over the active ``product_card``
subtree (``path @> ARRAY[:category_id]``) joined to ``product_attribute`` — no
Redis index, so bulk imports cannot drift the counts.
"""

from decimal import Decimal

from sqlalchemy import Row, and_, bindparam, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import BigInteger

from app.models.catalog import (
    Attribute,
    AttributeTranslation,
    AttributeValue,
    AttributeValueTranslation,
    Category,
    CategoryTranslation,
    Product,
    ProductAttribute,
    ProductCard,
    ProductTranslation,
)

# tsearch config per request language (§6.1). Unknown langs fall back to
# ``simple`` (no stemming) — but ``lang`` is already validated upstream.
TS_CONFIG_BY_LANG: dict[str, str] = {"ru": "russian", "ro": "romanian"}
DEFAULT_TS_CONFIG: str = "simple"

# Relevance floor assigned to an article-code (``product.code``) match so exact
# SKU hits sort above prose matches regardless of ``ts_rank`` magnitude (§6.1).
CODE_MATCH_RANK: float = 1_000_000.0


def ts_config_for(lang: str) -> str:
    """Map a request language to its PostgreSQL text-search config name (§6.1).

    Args:
        lang: Request language code (already validated to ``ALLOWED_LANGS``).

    Returns:
        str: The ``regconfig`` name (``russian`` / ``romanian`` / ``simple``).
    """
    return TS_CONFIG_BY_LANG.get(lang, DEFAULT_TS_CONFIG)


class SearchRepository:
    """Read queries for full-text search and live facets, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    # ------------------------------------------------------------------ #
    # Search: rank product_id s (FTS over translations + code match)
    # ------------------------------------------------------------------ #
    async def search_product_ids(
        self,
        query: str,
        lang: str,
    ) -> list[tuple[int, float]]:
        """Return ``(product_id, rank)`` matches for a query, ranked descending.

        Two signals are combined and de-duplicated per product, keeping the
        highest rank:

        * FTS over ``product_translation`` for ``lang`` — vector already built by
          the trigger with the matching config, so the query side uses the same
          config via :func:`ts_config_for`; relevance is ``ts_rank``.
        * Article-code match on ``product.code`` (case-insensitive), scored with
          :data:`CODE_MATCH_RANK` so exact SKU hits float to the top.

        Only active products are considered (inactive never surface).

        Args:
            query: The raw user query string.
            lang: Request language code (selects the tsearch config).

        Returns:
            list[tuple[int, float]]: ``(product_id, rank)`` ordered by rank desc,
                then ``product_id`` for a stable total order.
        """
        cfg = ts_config_for(lang)
        tsquery = func.websearch_to_tsquery(cfg, query)
        rank = func.ts_rank(ProductTranslation.search_vector, tsquery)

        fts_stmt = (
            select(
                ProductTranslation.product_id.label("product_id"),
                rank.label("rank"),
            )
            .join(Product, Product.id == ProductTranslation.product_id)
            .where(
                ProductTranslation.lang == lang,
                ProductTranslation.search_vector.op("@@")(tsquery),
                Product.is_active.is_(True),
            )
        )
        fts_rows = (await self.session.execute(fts_stmt)).all()

        # Article-code match (case-insensitive), outside the FTS — not translated.
        code_stmt = select(Product.id).where(
            Product.is_active.is_(True),
            Product.code.ilike(query.strip()),
        )
        code_ids = (await self.session.execute(code_stmt)).scalars().all()

        # Merge both signals, keeping the highest rank per product.
        ranked: dict[int, float] = {}
        for product_id, rank_value in fts_rows:
            ranked[product_id] = max(ranked.get(product_id, 0.0), float(rank_value))
        for product_id in code_ids:
            ranked[product_id] = max(ranked.get(product_id, 0.0), CODE_MATCH_RANK)

        return sorted(ranked.items(), key=lambda item: (-item[1], item[0]))

    async def load_cards(
        self,
        product_ids: list[int],
        lang: str,
    ) -> dict[int, ProductCard]:
        """Hydrate listing cards for the given product ids in one query (no N+1).

        Args:
            product_ids: Product ids to hydrate (in any order).
            lang: Request language code (``product_card`` is keyed by lang).

        Returns:
            dict[int, ProductCard]: Map ``product_id -> card`` for active cards
                that exist in the language (missing ids are simply absent).
        """
        if not product_ids:
            return {}
        stmt = select(ProductCard).where(
            ProductCard.lang == lang,
            ProductCard.is_active.is_(True),
            ProductCard.product_id.in_(product_ids),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {card.product_id: card for card in rows}

    # ------------------------------------------------------------------ #
    # Facets (§6.2): live aggregates over the active product_card subtree
    # ------------------------------------------------------------------ #
    @staticmethod
    def _subtree_card_ids(category_id: int, lang: str):
        """Subquery of active ``product_id`` s in a category subtree (one lang).

        Uses ``product_card.path @> ARRAY[:category_id]`` — the same subtree
        predicate the listing uses (§5.2) — restricted to a single language row
        per product so counts are not doubled across ru/ro.

        Args:
            category_id: Root category of the subtree.
            lang: Request language code (selects one card row per product).

        Returns:
            A scalar subquery yielding matching ``product_id`` s.
        """
        return (
            select(ProductCard.product_id)
            .where(
                ProductCard.is_active.is_(True),
                ProductCard.lang == lang,
                ProductCard.path.op("@>")(
                    bindparam(
                        "subtree_cat",
                        [category_id],
                        type_=ARRAY(BigInteger),
                    )
                ),
            )
            .scalar_subquery()
        )

    async def facet_values(
        self,
        category_id: int,
        lang: str,
    ) -> list[Row]:
        """Return attribute-value counts for a category subtree (§6.2).

        Aggregates ``product_attribute`` for active products in the subtree,
        joined to the attribute dictionary and localized names/values, producing
        one row per ``(attribute, value)`` with its product count.

        Args:
            category_id: Root category of the subtree to facet.
            lang: Request language code (for attribute/value names + card subtree).

        Returns:
            list[Row]: Rows of ``(attribute_id, code, attr_name, value_id, value,
                count)`` ordered by attribute code then value.
        """
        subtree = self._subtree_card_ids(category_id, lang)
        stmt = (
            select(
                Attribute.id.label("attribute_id"),
                Attribute.code.label("code"),
                AttributeTranslation.name.label("attr_name"),
                AttributeValue.id.label("value_id"),
                AttributeValueTranslation.value.label("value"),
                func.count().label("count"),
            )
            .select_from(ProductAttribute)
            .join(Product, Product.id == ProductAttribute.product_id)
            .join(AttributeValue, AttributeValue.id == ProductAttribute.value_id)
            .join(Attribute, Attribute.id == AttributeValue.attribute_id)
            .join(
                AttributeTranslation,
                and_(
                    AttributeTranslation.attribute_id == Attribute.id,
                    AttributeTranslation.lang == lang,
                ),
            )
            .join(
                AttributeValueTranslation,
                and_(
                    AttributeValueTranslation.value_id == AttributeValue.id,
                    AttributeValueTranslation.lang == lang,
                ),
            )
            .where(
                Product.is_active.is_(True),
                ProductAttribute.product_id.in_(subtree),
            )
            .group_by(
                Attribute.id,
                Attribute.code,
                AttributeTranslation.name,
                AttributeValue.id,
                AttributeValueTranslation.value,
            )
            .order_by(Attribute.code, AttributeValueTranslation.value)
        )
        return list((await self.session.execute(stmt)).all())

    async def price_bounds(
        self,
        category_id: int,
        lang: str,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Return ``(min_price, max_price)`` for the active subtree (§6.2).

        Args:
            category_id: Root category of the subtree.
            lang: Request language code (selects one ``product_card`` row/product).

        Returns:
            tuple[Decimal | None, Decimal | None]: Price bounds, ``(None, None)``
                when the subtree has no active products.
        """
        stmt = select(
            func.min(ProductCard.price),
            func.max(ProductCard.price),
        ).where(
            ProductCard.is_active.is_(True),
            ProductCard.lang == lang,
            ProductCard.path.op("@>")(
                bindparam(
                    "subtree_price",
                    [category_id],
                    type_=ARRAY(BigInteger),
                )
            ),
        )
        row = (await self.session.execute(stmt)).one()
        return row[0], row[1]

    # ------------------------------------------------------------------ #
    # Category resolution (facets are addressed by localized slug)
    # ------------------------------------------------------------------ #
    async def get_category_id_by_slug(self, slug: str, lang: str) -> int | None:
        """Resolve an active category id from its localized slug (§6.2 endpoint).

        Args:
            slug: Localized category slug.
            lang: Request language code.

        Returns:
            int | None: The category id, or ``None`` if no active match exists.
        """
        stmt = (
            select(Category.id)
            .join(CategoryTranslation, CategoryTranslation.category_id == Category.id)
            .where(
                CategoryTranslation.lang == lang,
                CategoryTranslation.slug == slug,
                Category.is_active.is_(True),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
