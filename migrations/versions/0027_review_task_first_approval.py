"""First-approval columns for dual-approval support (REV-012).

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_tasks", sa.Column("first_approved_by", sa.String(200)))
    op.add_column("review_tasks", sa.Column("first_approved_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("review_tasks", "first_approved_at")
    op.drop_column("review_tasks", "first_approved_by")
