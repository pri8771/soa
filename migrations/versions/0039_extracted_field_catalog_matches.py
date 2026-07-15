"""Persist reproducible catalog-match decisions on extracted fields.

Revision ID: 0039
Revises: 0038
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extracted_fields", sa.Column("catalog_match", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("extracted_fields", "catalog_match")
