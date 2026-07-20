"""Pydantic v2 read-schemas for the catalog (stage B2).

Covers the API contract of §4:

* Category tree nodes and category detail (name/slug/breadcrumbs/children/seo).
* Listing card (projected from ``product_card``) and the cursor-paginated
  listing envelope ``{data, next_cursor}`` (§5.3 keyset pagination).
* Product detail page (PDP): product fields, grouped attributes, media and both
  language slugs for the on-page language switcher (§2.1.1).

These are response-only DTOs — no god-DTO, only fields the storefront needs.
"""

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
    in_stock: bool
    category_id: int
    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    attributes: list[AttributeGroup] = Field(default_factory=list)
    media: list[MediaOut] = Field(default_factory=list)
    # Both language slugs so the frontend can build hreflang / a language switcher.
    slugs: list[ProductSlug] = Field(default_factory=list)


CategoryNode.model_rebuild()
