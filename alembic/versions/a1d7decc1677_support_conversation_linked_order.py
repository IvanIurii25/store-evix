"""support conversation linked order

Revision ID: a1d7decc1677
Revises: 4e24d5d439d7
Create Date: 2026-07-25 12:36:14.692641

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1d7decc1677'
down_revision: str | None = '4e24d5d439d7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only the new nullable FK column linked_order_id + its constraint. The
    # spurious ix_category_* / ix_product_* index drops autogenerate proposed are
    # pre-existing indexes created directly in earlier migrations and not mirrored
    # on the models — they must stay untouched.
    op.add_column('support_conversation', sa.Column('linked_order_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(op.f('fk_support_conversation_linked_order_id_order'), 'support_conversation', 'order', ['linked_order_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(op.f('fk_support_conversation_linked_order_id_order'), 'support_conversation', type_='foreignkey')
    op.drop_column('support_conversation', 'linked_order_id')
