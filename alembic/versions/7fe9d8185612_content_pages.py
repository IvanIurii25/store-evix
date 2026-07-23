"""content pages

Revision ID: 7fe9d8185612
Revises: d4e5f6a7b8c9
Create Date: 2026-07-23 14:21:12.421517

Hand-cleaned from autogenerate: keeps only the two new content-page tables and
drops the spurious ``drop_index`` operations autogenerate emitted for
hand-created (not ORM-modeled) indexes on ``category`` / ``product`` /
``product_attribute`` / ``product_translation``.

Adds the CMS-lite content-page tables (info/legal pages the storefront renders
and the admin edits). No rows are seeded here — a later data-migration seeds the
content.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7fe9d8185612"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_page",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("show_in_footer", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_page")),
        sa.UniqueConstraint("slug", name=op.f("uq_content_page_slug")),
    )
    op.create_table(
        "content_page_translation",
        sa.Column("page_id", sa.BigInteger(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("seo_description", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "lang IN ('ru', 'ro')",
            name=op.f("ck_content_page_translation_lang_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["content_page.id"],
            name=op.f("fk_content_page_translation_page_id_content_page"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "page_id", "lang", name=op.f("pk_content_page_translation")
        ),
    )


def downgrade() -> None:
    op.drop_table("content_page_translation")
    op.drop_table("content_page")
