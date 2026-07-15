"""Pin every model-call configuration input on processing runs.

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processing_runs", sa.Column("instruction_version_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "processing_runs",
        sa.Column("confidence_policy_version_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "processing_runs",
        sa.Column("provider_policy_version_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "processing_runs", sa.Column("provider_credential_ref", sa.Text(), nullable=True)
    )
    op.add_column(
        "processing_runs", sa.Column("execution_fingerprint", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_processing_runs_execution_fingerprint",
        "processing_runs",
        ["execution_fingerprint"],
    )
    op.add_column(
        "extracted_fields", sa.Column("instruction_reference", sa.String(200), nullable=True)
    )
    op.add_column(
        "extracted_fields", sa.Column("config_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "extracted_fields", sa.Column("execution_fingerprint", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("extracted_fields", "execution_fingerprint")
    op.drop_column("extracted_fields", "config_fingerprint")
    op.drop_column("extracted_fields", "instruction_reference")
    op.drop_index("ix_processing_runs_execution_fingerprint", table_name="processing_runs")
    op.drop_column("processing_runs", "execution_fingerprint")
    op.drop_column("processing_runs", "provider_credential_ref")
    op.drop_column("processing_runs", "provider_policy_version_id")
    op.drop_column("processing_runs", "confidence_policy_version_id")
    op.drop_column("processing_runs", "instruction_version_id")
