"""Processing-run and stage-run tables with RLS (PRC-001).

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def _rls(table: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def upgrade() -> None:
    op.create_table(
        "processing_runs",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stream_version_id", _guid(), nullable=True),
        sa.Column("config_fingerprint", sa.String(64), nullable=True),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("triggered_by", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False),
        sa.Column("total_cost_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "run_number", name="uq_processing_runs_document"),
    )
    op.create_index("ix_processing_runs_organization_id", "processing_runs", ["organization_id"])
    op.create_index("ix_processing_runs_document_id", "processing_runs", ["document_id"])
    op.create_index("ix_processing_runs_org_state", "processing_runs", ["organization_id", "state"])
    _rls("processing_runs")

    op.create_table(
        "stage_runs",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("run_id", _guid(), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_cents", sa.Integer(), nullable=False),
        sa.Column("safe_error", sa.String(500), nullable=True),
        sa.Column("failure_class", sa.String(20), nullable=True),
        sa.Column(
            "output_summary",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "stage", "attempt", name="uq_stage_runs_attempt"),
    )
    op.create_index("ix_stage_runs_organization_id", "stage_runs", ["organization_id"])
    op.create_index("ix_stage_runs_run_id", "stage_runs", ["run_id"])
    _rls("stage_runs")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON stage_runs")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON processing_runs")
    op.drop_index("ix_stage_runs_run_id", table_name="stage_runs")
    op.drop_index("ix_stage_runs_organization_id", table_name="stage_runs")
    op.drop_table("stage_runs")
    op.drop_index("ix_processing_runs_org_state", table_name="processing_runs")
    op.drop_index("ix_processing_runs_document_id", table_name="processing_runs")
    op.drop_index("ix_processing_runs_organization_id", table_name="processing_runs")
    op.drop_table("processing_runs")
