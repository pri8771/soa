"""Fence direct-upload verification outside database transactions.

Revision ID: 0052
Revises: 0051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_sessions",
        sa.Column("verification_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "upload_sessions",
        sa.Column("verification_token", sa.Uuid(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_sessions", "verification_token")
    op.drop_column("upload_sessions", "verification_started_at")
