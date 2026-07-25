"""support message attachments

Revision ID: 86d06fe6750a
Revises: e5f6a7b8c9d0
Create Date: 2026-07-25 22:19:37.183603

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '86d06fe6750a'
down_revision: str | None = 'e5f6a7b8c9d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only the three new nullable attachment columns on support_message plus the
    # attachment_kind check constraint. The spurious ix_category_* / ix_product_*
    # index drops autogenerate proposed are pre-existing indexes created directly
    # in earlier migrations and not mirrored on the models — they must stay
    # untouched. Autogenerate does not emit check constraints, so it is added by
    # hand here.
    op.add_column('support_message', sa.Column('attachment_kind', sa.String(), nullable=True))
    op.add_column('support_message', sa.Column('attachment_key', sa.String(), nullable=True))
    op.add_column('support_message', sa.Column('attachment_name', sa.String(), nullable=True))
    op.create_check_constraint(
        op.f('ck_support_message_support_attachment_kind_valid'),
        'support_message',
        "attachment_kind IS NULL OR attachment_kind IN ('photo', 'document')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('ck_support_message_support_attachment_kind_valid'),
        'support_message',
        type_='check',
    )
    op.drop_column('support_message', 'attachment_name')
    op.drop_column('support_message', 'attachment_key')
    op.drop_column('support_message', 'attachment_kind')
