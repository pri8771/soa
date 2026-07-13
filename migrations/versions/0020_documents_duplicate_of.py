"""Duplicate marker on documents (ING-006).

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("duplicate_of", sa.Uuid(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "duplicate_of")
