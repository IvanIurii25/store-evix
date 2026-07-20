"""order delivery address snapshot

Revision ID: 3265425b6a22
Revises: 5bbc92d87f34
Create Date: 2026-07-20 22:31:49.043564

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3265425b6a22'
down_revision: str | None = '5bbc92d87f34'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add nullable delivery-address snapshot columns (courier). Autogenerate also
    # proposed dropping several functional/partial indexes it cannot introspect
    # (category path/parent, product partial indexes, FTS gin) — those are false
    # positives and were removed; this migration only adds columns.
    op.add_column('order', sa.Column('delivery_name', sa.String(), nullable=True))
    op.add_column('order', sa.Column('delivery_city', sa.String(), nullable=True))
    op.add_column('order', sa.Column('delivery_street', sa.String(), nullable=True))
    op.add_column('order', sa.Column('delivery_zip', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('order', 'delivery_zip')
    op.drop_column('order', 'delivery_street')
    op.drop_column('order', 'delivery_city')
    op.drop_column('order', 'delivery_name')
