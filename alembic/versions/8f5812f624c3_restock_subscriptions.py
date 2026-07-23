"""restock subscriptions

Revision ID: 8f5812f624c3
Revises: 7fe9d8185612
Create Date: 2026-07-23 17:07:20.424582

Hand-cleaned from autogenerate: keeps only the new ``restock_subscription``
table (+ its two indexes) and drops the spurious ``drop_index`` operations
autogenerate emitted for hand-created (not ORM-modeled) indexes on
``category`` / ``product`` / ``product_attribute`` / ``product_translation``.

Adds the restock-notification subscription table (§2): one active subscription
per ``(product_id, user_id)`` with CASCADE FKs to product and app_user, a lang
CHECK (ru|ro), a status CHECK (active|notified) and two lookup indexes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f5812f624c3"
down_revision: str | None = "7fe9d8185612"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "restock_subscription",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "lang IN ('ru', 'ro')",
            name=op.f("ck_restock_subscription_lang_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'notified')",
            name=op.f("ck_restock_subscription_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            name=op.f("fk_restock_subscription_product_id_product"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_restock_subscription_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_restock_subscription")),
        sa.UniqueConstraint(
            "product_id",
            "user_id",
            name=op.f("uq_restock_subscription_product_id"),
        ),
    )
    op.create_index(
        "ix_restock_subscription_product_status",
        "restock_subscription",
        ["product_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_restock_subscription_user",
        "restock_subscription",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_restock_subscription_user", table_name="restock_subscription")
    op.drop_index(
        "ix_restock_subscription_product_status",
        table_name="restock_subscription",
    )
    op.drop_table("restock_subscription")
