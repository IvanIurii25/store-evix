"""per-language link on banner_translation

Revision ID: e4002343678f
Revises: dc84d7c53cf7
Create Date: 2026-08-01

Storefront paths carry the locale (/ru/p/… vs /ro/p/…), so one link per banner
sent half the visitors to the other language. The banner-level ``link_url``
stays as the fallback, so existing banners keep working untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4002343678f"
down_revision: str | None = "dc84d7c53cf7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("banner_translation", sa.Column("link_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("banner_translation", "link_url")
