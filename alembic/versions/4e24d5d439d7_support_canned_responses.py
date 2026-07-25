"""support canned responses

Revision ID: 4e24d5d439d7
Revises: 739998732fab
Create Date: 2026-07-25 12:06:32.609552

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4e24d5d439d7'
down_revision: str | None = '739998732fab'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only the new support_canned_response table + its index. The spurious index
    # drops autogenerate proposed (ix_category_*, ix_product_*) are pre-existing
    # indexes created directly in earlier migrations and not mirrored on the
    # models — they must stay untouched.
    op.create_table(
        "support_canned_response",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("'0'"),
            nullable=False,
        ),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lang IN ('ro', 'ru')",
            name=op.f("ck_support_canned_response_canned_lang_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_canned_response")),
    )
    op.create_index(
        "ix_support_canned_lang_sort",
        "support_canned_response",
        ["lang", "sort_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_support_canned_lang_sort", table_name="support_canned_response"
    )
    op.drop_table("support_canned_response")
