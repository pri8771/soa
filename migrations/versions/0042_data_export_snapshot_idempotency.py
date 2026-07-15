"""Snapshot and idempotency metadata for durable data exports.

Revision ID: 0042
Revises: 0041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_export_jobs",
        sa.Column("request_key", sa.String(200), nullable=True),
    )
    op.add_column(
        "data_export_jobs",
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "uq_data_export_jobs_org_request_key",
        "data_export_jobs",
        ["organization_id", "request_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_data_export_jobs_org_request_key", table_name="data_export_jobs")
    op.drop_column("data_export_jobs", "snapshot_at")
    op.drop_column("data_export_jobs", "request_key")
