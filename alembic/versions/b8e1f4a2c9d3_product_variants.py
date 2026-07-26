"""product variations (variable products)

Revision ID: b8e1f4a2c9d3
Revises: f7a1c2d3e4b5
Create Date: 2026-07-26 15:00:00.000000

Adds the schema backing WooCommerce-style variable products (§ variants), purely
additive — existing simple products are untouched (``product.has_variants``
defaults to false, no variant rows, price/stock stay on ``product``):

* ``product.has_variants`` — discriminator flag.
* ``product_variant`` — one purchasable combination per row (own price/old_price/
  qty/SKU); for variable products this holds the buy-time price and the race-safe
  stock authority. ``code`` is unique-when-present (NULLs distinct in Postgres).
* ``product_variant_value`` — the attribute values that define each variant.
* ``product_variation_attribute`` — which attributes are variation selectors
  (vs the informational ``product_attribute`` links).
* ``media.variant_id`` — optional per-variant image (NULL = shared gallery).
* ``product_card.price_max`` — max variant price for the storefront "from–to"
  range (NULL for simple / uniform-price products; ``price`` holds the min).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e1f4a2c9d3"
down_revision: str | None = "f7a1c2d3e4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product",
        sa.Column(
            "has_variants",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    op.create_table(
        "product_variant",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("old_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            name=op.f("fk_product_variant_product_id_product"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_variant")),
        sa.UniqueConstraint("code", name=op.f("uq_product_variant_code")),
    )
    op.create_index(
        op.f("ix_product_variant_product_id"),
        "product_variant",
        ["product_id"],
        unique=False,
    )

    op.create_table(
        "product_variant_value",
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("value_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["product_variant.id"],
            name=op.f("fk_product_variant_value_variant_id_product_variant"),
        ),
        sa.ForeignKeyConstraint(
            ["value_id"],
            ["attribute_value.id"],
            name=op.f("fk_product_variant_value_value_id_attribute_value"),
        ),
        sa.PrimaryKeyConstraint(
            "variant_id", "value_id", name=op.f("pk_product_variant_value")
        ),
    )

    op.create_table(
        "product_variation_attribute",
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("attribute_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            name=op.f("fk_product_variation_attribute_product_id_product"),
        ),
        sa.ForeignKeyConstraint(
            ["attribute_id"],
            ["attribute.id"],
            name=op.f("fk_product_variation_attribute_attribute_id_attribute"),
        ),
        sa.PrimaryKeyConstraint(
            "product_id",
            "attribute_id",
            name=op.f("pk_product_variation_attribute"),
        ),
    )

    op.add_column(
        "media", sa.Column("variant_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_media_variant_id_product_variant"),
        "media",
        "product_variant",
        ["variant_id"],
        ["id"],
    )

    op.add_column(
        "product_card", sa.Column("price_max", sa.Numeric(12, 2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("product_card", "price_max")
    op.drop_constraint(
        op.f("fk_media_variant_id_product_variant"), "media", type_="foreignkey"
    )
    op.drop_column("media", "variant_id")
    op.drop_table("product_variation_attribute")
    op.drop_table("product_variant_value")
    op.drop_index(
        op.f("ix_product_variant_product_id"), table_name="product_variant"
    )
    op.drop_table("product_variant")
    op.drop_column("product", "has_variants")
