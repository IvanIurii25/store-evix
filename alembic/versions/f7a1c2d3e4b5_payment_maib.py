"""payment table (maib card payments)

Revision ID: f7a1c2d3e4b5
Revises: 838b3223574e
Create Date: 2026-07-26 12:00:00.000000

Adds the ``payment`` table backing the env-gated card-payment (maib) flow: one
row per card payment attempt against an order, with a unique ``pay_id`` for
callback idempotency, a local status CHECK, a provider CHECK, a ``raw`` JSONB
snapshot of the last provider payload, and a CASCADE FK to ``order``. COD orders
create no payment row, so this is purely additive to the existing money-path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a1c2d3e4b5"
down_revision: str | None = "838b3223574e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("pay_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "provider IN ('maib')",
            name=op.f("ck_payment_payment_provider_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ok', 'failed', 'refunded')",
            name=op.f("ck_payment_payment_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["order.id"],
            name=op.f("fk_payment_order_id_order"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment")),
        sa.UniqueConstraint("pay_id", name=op.f("uq_payment_pay_id")),
    )
    op.create_index(
        "ix_payment_order_id",
        "payment",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payment_order_id", table_name="payment")
    op.drop_table("payment")
