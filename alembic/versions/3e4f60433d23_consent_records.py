"""consent records

Revision ID: 3e4f60433d23
Revises: 8f5812f624c3
Create Date: 2026-07-24 14:58:16.746405

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3e4f60433d23"
down_revision: str | None = "8f5812f624c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only the new consent_record table. The spurious index drops autogenerate
    # proposed are pre-existing indexes created directly in migration
    # 928ef6e9feec (not mirrored on the models) — they must stay.
    op.create_table(
        "consent_record",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("anonymous_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.CheckConstraint(
            "action IN ('accept_all', 'reject_all', 'custom', 'withdraw')",
            name=op.f("ck_consent_record_consent_action_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('banner', 'settings')",
            name=op.f("ck_consent_record_consent_source_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_consent_record_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_record")),
    )


def downgrade() -> None:
    op.drop_table("consent_record")
