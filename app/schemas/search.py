"""Pydantic v2 response schemas for search + facets (stage B3).

Covers the API contract of §4 (``/search``) and §6.2 (facets):

* :class:`SearchResponse` — the ranked, page-paginated search result envelope
  ``{data, total, page, page_size}`` (§4: ``/search?...&page``). Each item is a
  listing card (reused from the catalog) plus its relevance ``rank``.
* :class:`FacetsResponse` — live-computed facets for a category subtree (§6.2):
  attribute values with counts, grouped per attribute, plus the price bounds.

These are response-only DTOs — only the fields the storefront needs.
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.catalog import ProductCardOut


class SearchHit(BaseModel):
    """A single search result: a listing card plus its relevance rank.

    ``rank`` combines the FTS ``ts_rank`` with the article-code boost; higher is
    more relevant. It is exposed for debugging/observability, not for display.
    """

    card: ProductCardOut
    rank: float


class SearchResponse(BaseModel):
    """Page-paginated, relevance-ranked search envelope (§4).

    Uses classic ``{data, total, page, page_size}`` pagination (not keyset): FTS
    relevance order is not a stable monotonic key, so keyset cursors do not apply
    here. ``data`` is ordered by descending relevance.
    """

    data: list[SearchHit] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class FacetValue(BaseModel):
    """One selectable attribute value with its product count in the subtree."""

    value_id: int
    value: str
    count: int


class FacetAttribute(BaseModel):
    """One attribute with its localized name and available values (+counts)."""

    attribute_id: int
    code: str
    name: str
    values: list[FacetValue] = Field(default_factory=list)


class FacetsResponse(BaseModel):
    """Live facets for a category subtree (§6.2).

    Attributes:
        attributes: Attribute facets (values + counts) available in the subtree.
        price_min: Lowest active-product price in the subtree (``None`` if empty).
        price_max: Highest active-product price in the subtree (``None`` if empty).
    """

    attributes: list[FacetAttribute] = Field(default_factory=list)
    price_min: Decimal | None = None
    price_max: Decimal | None = None
