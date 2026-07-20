"""order_number_seq

Revision ID: 5bbc92d87f34
Revises: 928ef6e9feec
Create Date: 2026-07-20 11:29:02.395882

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5bbc92d87f34"
down_revision: str | None = "928ef6e9feec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Human-readable order number "YYYYMMDD-<seq>" (§ Open gaps). A global
    # sequence is atomic (race-safe) — it is not reset per day; the date is a
    # readable prefix, uniqueness comes from the monotonic sequence.
    op.execute("CREATE SEQUENCE IF NOT EXISTS order_number_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS order_number_seq")
