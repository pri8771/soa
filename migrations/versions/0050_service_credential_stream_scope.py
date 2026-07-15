"""Bind service credentials to explicit stream allowlists.

Revision ID: 0050
Revises: 0049
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    portable_json = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    # Existing unscoped keys intentionally migrate to an empty allowlist and
    # therefore fail closed. Operators must replace them through the managed
    # API before public ingestion resumes.
    op.add_column(
        "service_credentials",
        sa.Column(
            "allowed_stream_ids",
            portable_json,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("service_credentials", "allowed_stream_ids")
