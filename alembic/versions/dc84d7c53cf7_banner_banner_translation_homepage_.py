"""banner + banner_translation (homepage carousel)

Revision ID: dc84d7c53cf7
Revises: d3e4f5a6b7c8
Create Date: 2026-08-01 20:20:42.413762

Autogenerate also proposed dropping fifteen indexes (listing, FTS, cart, order,
category-path GIN …). Those are created by hand in earlier migrations and are
absent from the model metadata, so every autogenerate sees them as "removed".
They are load-bearing in production and are deliberately not touched here.

Also adds a partial index for the public carousel read: active banners in
display order, which is the only query the storefront runs on this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dc84d7c53cf7"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "banner",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("link_url", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_banner")),
    )
    op.create_table(
        "banner_translation",
        sa.Column("banner_id", sa.BigInteger(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("image_mobile_url", sa.Text(), nullable=True),
        sa.Column("alt", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("subtitle", sa.Text(), nullable=True),
        sa.Column("cta_label", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "lang IN ('ru', 'ro')", name=op.f("ck_banner_translation_lang_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["banner_id"],
            ["banner.id"],
            name=op.f("fk_banner_translation_banner_id_banner"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "banner_id", "lang", name=op.f("pk_banner_translation")
        ),
    )
    op.create_index(
        "ix_banner_live",
        "banner",
        ["position", "id"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_banner_live", table_name="banner", postgresql_where=sa.text("is_active")
    )
    op.drop_table("banner_translation")
    op.drop_table("banner")
