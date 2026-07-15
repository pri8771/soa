"""Durable evaluation runs.

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("stream_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("candidate_fingerprint", sa.String(64), nullable=False, index=True),
        sa.Column("dataset_version_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("baseline_run_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("predictions", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("gate_result", sa.JSON(), nullable=True),
        sa.Column("safe_error", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    if op.get_bind().dialect.name == "postgresql":
        tenant = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
        op.execute("ALTER TABLE evaluation_runs ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE evaluation_runs FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation ON evaluation_runs "
            f"USING (organization_id = {tenant}) WITH CHECK (organization_id = {tenant})"
        )


def downgrade() -> None:
    op.drop_table("evaluation_runs")
