"""Baseline — establishes the migration chain.

Revision ID: 0001
Revises:
Create Date: 2026-07-12
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentionally empty: proves the migration machinery end to end before
    # the first real schema change (DB-004 outbox, DB-005 audit events).
    pass


def downgrade() -> None:
    pass
