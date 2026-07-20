"""Pydantic v2 request/response DTOs for the thin catalog admin-API (stage B7).

These are back-office contracts — deliberately narrow and explicit (no generic
CRUD engine, §10). They map the write side of the catalog: categories (+
translations, reorder, move), products (+ translations, media, attribute links),
attributes (+ values + translations) and product search.

Language codes are validated against :data:`~app.models.catalog.ALLOWED_LANGS`
so an unknown ``lang`` is rejected at the edge, not by a DB CheckConstraint.
"""

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.catalog import ALLOWED_LANGS

# Reused length bounds so payloads fail fast with a clear 422 instead of hitting
# the database. Slugs/codes are short identifiers; free text is bounded loosely.
_CODE_MAX: int = 128
_SLUG_MAX: int = 255
_NAME_MAX: int = 512

# A URL slug must be lowercase alphanumeric words joined by single hyphens
# (no leading/trailing/double hyphens). Enforced so product/category links are
# always descriptive — a bare single letter or digit (e.g. ``/p/s``) is rejected.
_SLUG_MIN: int = 2
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _validate_slug(value: str) -> str:
    """Normalize and validate a URL slug (§2.1.1).

    Whitespace is trimmed and the value is lowercased, then it must match a
    clean slug pattern and be at least two characters — so links can never
    degrade to a single letter or digit.

    Args:
        value: Candidate slug.

    Returns:
        str: The normalized, validated slug.

    Raises:
        ValueError: If the slug is too short or not a clean hyphenated slug.
    """
    slug = value.strip().lower()
    if len(slug) < _SLUG_MIN:
        raise ValueError(f"slug must be at least {_SLUG_MIN} characters")
    if not _SLUG_RE.match(slug):
        raise ValueError(
            "slug must be lowercase alphanumeric words separated by single hyphens"
        )
    return slug


def _validate_lang(value: str) -> str:
    """Reject any language code outside the supported set (§2.1.1).

    Args:
        value: Candidate language code.

    Returns:
        str: The validated language code.

    Raises:
        ValueError: If the code is not one of :data:`ALLOWED_LANGS`.
    """
    if value not in ALLOWED_LANGS:
        raise ValueError(f"lang must be one of {sorted(ALLOWED_LANGS)}")
    return value


# --------------------------------------------------------------------------- #
# Category
# --------------------------------------------------------------------------- #
class CategoryTranslationIn(BaseModel):
    """A single per-language translation payload for a category (§2.1.1)."""

    lang: str
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    slug: str = Field(min_length=_SLUG_MIN, max_length=_SLUG_MAX)
    seo_title: str | None = None
    seo_description: str | None = None

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        return _validate_slug(value)


class CategoryCreate(BaseModel):
    """Create a category with its initial translations (root..self path derived)."""

    parent_id: int | None = None
    position: int = 0
    is_active: bool = False
    translations: list[CategoryTranslationIn] = Field(default_factory=list)


class CategoryUpdate(BaseModel):
    """Partial update of a category's structural fields (translations excluded)."""

    parent_id: int | None = None
    position: int | None = None
    is_active: bool | None = None


class CategoryTranslationOut(BaseModel):
    """A category translation as returned to the back-office."""

    model_config = ConfigDict(from_attributes=True)

    lang: str
    name: str
    slug: str
    seo_title: str | None = None
    seo_description: str | None = None


class CategoryOut(BaseModel):
    """Full admin view of a category (structure + translations)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    parent_id: int | None
    path: list[int]
    depth: int
    position: int
    is_active: bool
    translations: list[CategoryTranslationOut] = Field(default_factory=list)


class CategoryReorderItem(BaseModel):
    """One ``(category_id, position)`` assignment in a reorder request."""

    category_id: int
    position: int


class CategoryReorderRequest(BaseModel):
    """Bulk position update for a set of sibling categories."""

    items: list[CategoryReorderItem] = Field(min_length=1)


class CategoryMoveRequest(BaseModel):
    """Move a category under a new parent (or to root when ``parent_id`` is null)."""

    parent_id: int | None = None


# --------------------------------------------------------------------------- #
# Attribute + values
# --------------------------------------------------------------------------- #
class AttributeTranslationIn(BaseModel):
    """Per-language display name for an attribute."""

    lang: str
    name: str = Field(min_length=1, max_length=_NAME_MAX)

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)


class AttributeCreate(BaseModel):
    """Create an attribute dictionary entry with its translations."""

    code: str = Field(min_length=1, max_length=_CODE_MAX)
    translations: list[AttributeTranslationIn] = Field(default_factory=list)


class AttributeUpdate(BaseModel):
    """Partial update of an attribute (code and/or translations)."""

    code: str | None = Field(default=None, min_length=1, max_length=_CODE_MAX)
    translations: list[AttributeTranslationIn] | None = None


class AttributeTranslationOut(BaseModel):
    """An attribute translation as returned to the back-office."""

    model_config = ConfigDict(from_attributes=True)

    lang: str
    name: str


class AttributeValueTranslationIn(BaseModel):
    """Per-language display text for an attribute value."""

    lang: str
    value: str = Field(min_length=1, max_length=_NAME_MAX)

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)


class AttributeValueCreate(BaseModel):
    """Create a concrete value under an attribute, with its translations."""

    translations: list[AttributeValueTranslationIn] = Field(default_factory=list)


class AttributeValueUpdate(BaseModel):
    """Partial update of an attribute value's translations."""

    translations: list[AttributeValueTranslationIn] | None = None


class AttributeValueTranslationOut(BaseModel):
    """An attribute-value translation as returned to the back-office."""

    model_config = ConfigDict(from_attributes=True)

    lang: str
    value: str


class AttributeValueOut(BaseModel):
    """An attribute value with its translations."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    attribute_id: int
    translations: list[AttributeValueTranslationOut] = Field(default_factory=list)


class AttributeOut(BaseModel):
    """Full admin view of an attribute (code + translations + values)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    translations: list[AttributeTranslationOut] = Field(default_factory=list)
    values: list[AttributeValueOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Product
# --------------------------------------------------------------------------- #
class ProductTranslationIn(BaseModel):
    """A single per-language translation payload for a product (§2.1.1)."""

    lang: str
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    slug: str = Field(min_length=_SLUG_MIN, max_length=_SLUG_MAX)
    description: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        return _validate_slug(value)


class ProductCreate(BaseModel):
    """Create a product with its initial translations (inactive by default)."""

    category_id: int
    code: str = Field(min_length=1, max_length=_CODE_MAX)
    price: Decimal = Field(ge=0)
    old_price: Decimal | None = Field(default=None, ge=0)
    qty: int = Field(default=0, ge=0)
    is_active: bool = False
    translations: list[ProductTranslationIn] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    """Partial update of a product's structural / price / stock fields.

    Setting ``is_active=True`` enforces the both-languages publication rule
    (§2.1.1) in the service before the card read-model is rebuilt.
    """

    category_id: int | None = None
    code: str | None = Field(default=None, min_length=1, max_length=_CODE_MAX)
    price: Decimal | None = Field(default=None, ge=0)
    old_price: Decimal | None = Field(default=None, ge=0)
    qty: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class ProductTranslationOut(BaseModel):
    """A product translation as returned to the back-office."""

    model_config = ConfigDict(from_attributes=True)

    lang: str
    name: str
    slug: str
    description: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None


class MediaAdminOut(BaseModel):
    """A stored product image row (v1: one url per row, no derivatives — §2.1)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    url: str
    kind: str
    position: int


class ProductOut(BaseModel):
    """Full admin view of a product (structure + translations + media)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int
    code: str
    price: Decimal
    old_price: Decimal | None = None
    qty: int
    is_active: bool
    translations: list[ProductTranslationOut] = Field(default_factory=list)
    media: list[MediaAdminOut] = Field(default_factory=list)
    value_ids: list[int] = Field(default_factory=list)


class ProductAttributeSetRequest(BaseModel):
    """Replace a product's attribute-value links with the given set."""

    value_ids: list[int] = Field(default_factory=list)


class ProductSearchItem(BaseModel):
    """A compact product row for the back-office search list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    price: Decimal
    is_active: bool
    name: str | None = None


class ProductSearchResult(BaseModel):
    """Envelope for a back-office product search response."""

    data: list[ProductSearchItem] = Field(default_factory=list)
