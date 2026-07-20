"""Catalog domain ORM models (stage B1c).

Maps §2.1 (catalog structure), §2.1.1 (i18n translation tables + publication
rule) and §5.2 (``product_card`` read-model) of the FlyStore backend analysis.

Conventions:

* Translatable fields (name/description/slug/seo) live in ``*_translation``
  tables keyed by ``(<entity>_id, lang)``. Structural / money / stock fields
  live on the main table.
* Language is stored as plain text constrained to :data:`ALLOWED_LANGS` via a
  ``CheckConstraint`` (no PostgreSQL ENUM type).
* ``product_card`` is a denormalized read projection (§5.2). It is **not** the
  source of truth — it is rebuilt by ``CatalogService.rebuild_card`` on write.

This module defines only ORM models. The FTS ``search_vector`` trigger and any
full-text indexing are the integrator's migration concern (§6.1) and are not
created here.
"""

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Supported languages for v1 (ru + ro). Applied as a CheckConstraint on every
# ``lang`` column instead of a PostgreSQL ENUM (§2.1.1).
ALLOWED_LANGS: tuple[str, ...] = ("ru", "ro")

# Rendered SQL fragment for the ``lang`` CheckConstraint, e.g. ``'ru', 'ro'``.
_LANG_IN = ", ".join(f"'{lang}'" for lang in ALLOWED_LANGS)

# Allowed ``media.kind`` values (v1: only images; video later — §2.1).
ALLOWED_MEDIA_KINDS: tuple[str, ...] = ("image",)
_MEDIA_KIND_IN = ", ".join(f"'{kind}'" for kind in ALLOWED_MEDIA_KINDS)


class Category(Base, TimestampMixin):
    """Catalog category (manual tree via ``parent_id``, not MPTT — §2.1)."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("category.id"),
        nullable=True,
    )
    # Denormalized breadcrumbs (root..self), maintained by the service on write.
    path: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CategoryTranslation(Base):
    """Per-language SEO/URL fields for a category (§2.1.1)."""

    __tablename__ = "category_translation"
    __table_args__ = (
        PrimaryKeyConstraint("category_id", "lang"),
        UniqueConstraint("lang", "slug"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    category_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("category.id"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    seo_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Product(Base, TimestampMixin):
    """Product: structural fields, price and stock (§2.1)."""

    __tablename__ = "product"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("category.id"),
        nullable=False,
    )
    # Article number, not translated.
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ProductTranslation(Base):
    """Per-language product content + FTS vector (§2.1.1).

    ``search_vector`` is populated by a DB trigger owned by the integrator's
    migration (§6.1); it is left NULL here.
    """

    __tablename__ = "product_translation"
    __table_args__ = (
        PrimaryKeyConstraint("product_id", "lang"),
        UniqueConstraint("lang", "slug"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)


class Attribute(Base):
    """Attribute dictionary entry (e.g. ``ram``, ``color`` — not translated)."""

    __tablename__ = "attribute"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class AttributeTranslation(Base):
    """Per-language display name for an attribute (§2.1.1)."""

    __tablename__ = "attribute_translation"
    __table_args__ = (
        PrimaryKeyConstraint("attribute_id", "lang"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    attribute_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attribute.id"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class AttributeValue(Base):
    """A concrete value of an attribute (translatable text in translation)."""

    __tablename__ = "attribute_value"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attribute_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attribute.id"),
        nullable=False,
    )


class AttributeValueTranslation(Base):
    """Per-language display text for an attribute value (§2.1.1)."""

    __tablename__ = "attribute_value_translation"
    __table_args__ = (
        PrimaryKeyConstraint("value_id", "lang"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    value_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attribute_value.id"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class ProductAttribute(Base):
    """M2M link between a product and an attribute value (§2.1)."""

    __tablename__ = "product_attribute"
    __table_args__ = (PrimaryKeyConstraint("product_id", "value_id"),)

    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id"),
        nullable=False,
    )
    value_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("attribute_value.id"),
        nullable=False,
    )


class Media(Base):
    """Product media. v1: a single ``url`` per row, no derivatives (§2.1)."""

    __tablename__ = "media"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_MEDIA_KIND_IN})", name="kind_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="image")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ProductCard(Base):
    """Denormalized listing-card read-model (§5.2).

    Keyed by ``(product_id, lang)`` with exactly two rows per active product
    (ru + ro), guaranteed by the publication rule (§2.1.1). This is a projection
    rebuilt by ``CatalogService.rebuild_card`` on write — **not** the source of
    truth. Do not write business logic against it as authoritative state.
    """

    __tablename__ = "product_card"
    __table_args__ = (
        PrimaryKeyConstraint("product_id", "lang"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id"),
        nullable=False,
    )
    lang: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    main_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    badge: Mapped[str | None] = mapped_column(String, nullable=True)
    path: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
