"""baseline (empty) — B0 skeleton has no domain models yet

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-20

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op baseline. Domain tables are introduced from B1 onward."""
    pass


def downgrade() -> None:
    """No-op baseline downgrade."""
    pass
