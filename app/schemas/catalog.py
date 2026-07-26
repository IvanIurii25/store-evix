"""Pydantic v2 read-schemas for the catalog (stage B2).

Covers the API contract of §4:

* Category tree nodes and category detail (name/slug/breadcrumbs/children/seo).
* Listing card (projected from ``product_card``) and the cursor-paginated
  listing envelope ``{data, next_cursor}`` (§5.3 keyset pagination).
* Product detail page (PDP): product fields, grouped attributes, media and both
  language slugs for the on-page language switcher (§2.1.1).

These are response-only DTOs — no god-DTO, only fields the storefront needs.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ProductSort(str, Enum):
    """Allowed sort orders for the category listing (§4)."""

    NEWEST = "newest"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"


class Breadcrumb(BaseModel):
    """A single breadcrumb entry (root..self order), built from ``category.path``."""

    id: int
    name: str
    slug: str


class CategoryNode(BaseModel):
    """A node in the category tree (navigation)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    parent_id: int | None
    name: str
    slug: str
    depth: int
    position: int
    cover_image_url: str | None = None
    children: list["CategoryNode"] = Field(default_factory=list)


class CategorySlug(BaseModel):
    """A ``(lang, slug)`` pair powering hreflang / the category switcher (§2.1.1)."""

    lang: str
    slug: str


class CategoryDetail(BaseModel):
    """Category detail: localized name/slug + breadcrumbs + children + seo (§4)."""

    id: int
    name: str
    slug: str
    seo_title: str | None = None
    seo_description: str | None = None
    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    children: list[CategoryNode] = Field(default_factory=list)
    # Both language slugs so the frontend can build hreflang / a language switcher.
    slugs: list[CategorySlug] = Field(default_factory=list)


class ProductCardOut(BaseModel):
    """Listing card, projected from the denormalized ``product_card`` (§5.2)."""

    model_config = ConfigDict(from_attributes=True)

    product_id: int
    name: str
    slug: str
    price: Decimal
    old_price: Decimal | None = None
    # For variable products: the max variant price when it differs from ``price``
    # (which holds the min) → the storefront renders "from {price}". NULL otherwise.
    price_max: Decimal | None = None
    in_stock: bool
    main_image_url: str | None = None
    badge: str | None = None


class ProductListing(BaseModel):
    """Cursor-paginated listing envelope: ``{data, next_cursor}`` (§5.3).

    ``next_cursor`` is an opaque token to pass back as ``?cursor=``; ``None``
    means the last page has been reached.
    """

    data: list[ProductCardOut]
    next_cursor: str | None = None


class MediaOut(BaseModel):
    """A single product image (v1: one url per row, no derivatives — §2.1)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    kind: str
    position: int


class AttributeGroup(BaseModel):
    """Grouped characteristic: one attribute and its resolved value(s) for the PDP."""

    code: str
    name: str
    values: list[str] = Field(default_factory=list)


class ProductSlug(BaseModel):
    """A ``(lang, slug)`` pair powering the on-page language switcher (§2.1.1)."""

    lang: str
    slug: str


class VariationValueOut(BaseModel):
    """One selectable option of a variation-defining attribute (with its id)."""

    value_id: int
    value: str


class VariationAttributeOut(BaseModel):
    """A variation selector: attribute + its selectable, localized options.

    The frontend renders one picker per entry and maps a full selection
    (one ``value_id`` per attribute) to a variant via ``VariantOut.value_ids``.
    """

    attribute_id: int
    code: str
    name: str
    values: list[VariationValueOut] = Field(default_factory=list)


class VariantOut(BaseModel):
    """One purchasable variant of a variable product (price/stock/options)."""

    id: int
    code: str | None = None
    price: Decimal
    old_price: Decimal | None = None
    in_stock: bool
    # The attribute values (one per variation attribute) that define this variant.
    value_ids: list[int] = Field(default_factory=list)
    image_url: str | None = None


class ProductDetail(BaseModel):
    """Product detail page (PDP): product + grouped attributes + media + slugs."""

    id: int
    code: str
    name: str
    slug: str
    description: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    price: Decimal
    old_price: Decimal | None = None
    # Variable products (§ variants). ``has_variants`` toggles the storefront
    # variant selector. ``price`` holds the min (the "from" price); ``price_max``
    # is set only when it differs → the PDP shows a "from–to" range. ``variants``
    # + ``variation_attributes`` let the frontend resolve a selection → price
    # client-side (no extra round-trip). All empty/False for simple products.
    has_variants: bool = False
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    variation_attributes: list[VariationAttributeOut] = Field(default_factory=list)
    variants: list[VariantOut] = Field(default_factory=list)
    in_stock: bool
    # Honest social-proof signal: real number of distinct *live* carts (draft
    # status) currently holding this product. Never randomized/inflated. The
    # storefront decides whether to surface it (shows only above a threshold);
    # the backend always returns the true count (0 when none).
    in_cart_count: int = 0
    # Approved-review rating summary for the PDP star widget + JSON-LD
    # aggregateRating (Reviews & Ratings §3). ``rating_avg`` is rounded to one
    # decimal and is ``None`` when the product has no approved reviews (then
    # ``rating_count`` is ``0``). Computed on the detail only (never on listings,
    # for performance) and additive to the existing contract.
    rating_avg: float | None = None
    rating_count: int = 0
    category_id: int
    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    attributes: list[AttributeGroup] = Field(default_factory=list)
    media: list[MediaOut] = Field(default_factory=list)
    # Both language slugs so the frontend can build hreflang / a language switcher.
    slugs: list[ProductSlug] = Field(default_factory=list)


CategoryNode.model_rebuild()


# --------------------------------------------------------------------------- #
# Sitemap (flat feed for per-locale sitemaps with hreflang alternates)
# --------------------------------------------------------------------------- #
class SitemapSlug(BaseModel):
    """A ``(lang, slug)`` pair for one indexable entity."""

    lang: str
    slug: str


class SitemapEntry(BaseModel):
    """One published entity: its per-language slugs + last-modified time."""

    slugs: list[SitemapSlug] = Field(default_factory=list)
    updated_at: datetime | None = None


class SitemapData(BaseModel):
    """All published, indexable entities for building per-locale sitemaps."""

    categories: list[SitemapEntry] = Field(default_factory=list)
    products: list[SitemapEntry] = Field(default_factory=list)
