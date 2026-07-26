"""variant buy-path columns (cart_item + order_item variant_id)

Revision ID: c9d2e5f7a1b4
Revises: b8e1f4a2c9d3
Create Date: 2026-07-26 16:30:00.000000

Threads the chosen variant through the buy path (§ variants, P2). Additive —
existing rows keep ``variant_id`` NULL and behave as before:

* ``cart_item.variant_id`` — the chosen variant for variable products (NULL for
  simple). The single ``(cart_id, product_id)`` unique constraint is replaced by
  two partial unique indexes (NULLs are distinct in Postgres, so one constraint
  spanning a nullable column would let simple products duplicate): one line per
  product when ``variant_id IS NULL``, one line per variant otherwise.
* ``order_item.variant_id`` — soft link (SET NULL) to the purchased variant.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d2e5f7a1b4"
down_revision: str | None = "b8e1f4a2c9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cart_item", sa.Column("variant_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_cart_item_variant_id_product_variant"),
        "cart_item",
        "product_variant",
        ["variant_id"],
        ["id"],
    )
    op.drop_constraint("cart_item_cart_product", "cart_item", type_="unique")
    op.create_index(
        "uq_cart_item_cart_product",
        "cart_item",
        ["cart_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("variant_id IS NULL"),
    )
    op.create_index(
        "uq_cart_item_cart_variant",
        "cart_item",
        ["cart_id", "variant_id"],
        unique=True,
        postgresql_where=sa.text("variant_id IS NOT NULL"),
    )

    op.add_column(
        "order_item", sa.Column("variant_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_order_item_variant_id_product_variant"),
        "order_item",
        "product_variant",
        ["variant_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_order_item_variant_id_product_variant"),
        "order_item",
        type_="foreignkey",
    )
    op.drop_column("order_item", "variant_id")

    op.drop_index("uq_cart_item_cart_variant", table_name="cart_item")
    op.drop_index("uq_cart_item_cart_product", table_name="cart_item")
    op.create_unique_constraint(
        "cart_item_cart_product", "cart_item", ["cart_id", "product_id"]
    )
    op.drop_constraint(
        op.f("fk_cart_item_variant_id_product_variant"),
        "cart_item",
        type_="foreignkey",
    )
    op.drop_column("cart_item", "variant_id")
