"""Record the implementation provenance that actually executed a run.

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PORTABLE_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "processing_runs",
        sa.Column("runtime_provenance", PORTABLE_JSON, nullable=True),
    )
    op.add_column(
        "processing_runs",
        sa.Column("runtime_fingerprint", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_processing_runs_runtime_fingerprint",
        "processing_runs",
        ["runtime_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processing_runs_runtime_fingerprint",
        table_name="processing_runs",
    )
    op.drop_column("processing_runs", "runtime_fingerprint")
    op.drop_column("processing_runs", "runtime_provenance")
