"""performance indexes: product_card listing, order/order_item, cart lookup

Revision ID: a7c3e9f1b2d5
Revises: c9d2e5f7a1b4
Create Date: 2026-07-27 00:00:00.000000

Additive hot-path indexes (no schema/data change). Each backs a verified query:

* ``ix_product_card_listing`` — storefront listing always filters ``lang`` +
  ``is_active`` and ranges/keysets on ``price, product_id``
  (``catalog_repo._apply_listing_filters`` / ``list_cards``).
* ``ix_order_user_created`` — a user's order history:
  ``WHERE user_id ORDER BY created_at DESC, id DESC`` (``list_orders_for_user``).
* ``ix_order_created`` — admin order list, always sorted ``created_at DESC,
  id DESC`` (``admin_order_repo``). The optional ``status`` filter is 4-valued /
  low-cardinality, so no dedicated status index — the planner filters over this.
* ``ix_order_item_order`` — order-line hydration by ``order_id`` (Postgres does
  not auto-index foreign keys).
* ``ix_cart_user_active`` / ``ix_cart_session_active`` — cart resolution on every
  cart request; partial on ``status = 'draft'`` (the only served state,
  ``cart_repo.ACTIVE_STATUS``) so the index matches the query exactly and stays
  small.

Deliberately NOT added: a GIN index on ``product_card.path``. The subtree filter
compiles to ``:cat = ANY(path)``, which Postgres cannot serve from GIN (that
needs the ``@>`` containment form). A GIN index here would be dead weight; if the
category filter becomes a bottleneck, switch the query to ``@>`` first, then add
the index in a follow-up.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1b2d5"
down_revision: str | None = "c9d2e5f7a1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_product_card_listing",
        "product_card",
        ["lang", "is_active", "price", "product_id"],
    )
    op.create_index(
        "ix_order_user_created",
        "order",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_order_created",
        "order",
        [sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_order_item_order",
        "order_item",
        ["order_id"],
    )
    op.create_index(
        "ix_cart_user_active",
        "cart",
        ["user_id"],
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_index(
        "ix_cart_session_active",
        "cart",
        ["session_token"],
        postgresql_where=sa.text("status = 'draft'"),
    )


def downgrade() -> None:
    op.drop_index("ix_cart_session_active", table_name="cart")
    op.drop_index("ix_cart_user_active", table_name="cart")
    op.drop_index("ix_order_item_order", table_name="order_item")
    op.drop_index("ix_order_created", table_name="order")
    op.drop_index("ix_order_user_created", table_name="order")
    op.drop_index("ix_product_card_listing", table_name="product_card")
