"""Export jobs and append-only delivery attempts with RLS (EXP-005).

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


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
        "export_jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("canonical_payload_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("integration_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("mapping_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("business_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "business_key"),
    )
    op.create_index("ix_export_jobs_state", "export_jobs", ["state"])
    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("export_job_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("safe_error", sa.String(500), nullable=True),
        sa.Column("request_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("export_job_id", "attempt_number"),
    )
    for table in ("export_jobs", "delivery_attempts"):
        _rls(table)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("delivery_attempts", "export_jobs"):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("delivery_attempts")
    op.drop_index("ix_export_jobs_state", table_name="export_jobs")
    op.drop_table("export_jobs")
