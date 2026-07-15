"""Durable large data exports.

Revision ID: 0041
Revises: 0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_export_jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("total_documents", sa.Integer(), nullable=False),
        sa.Column("processed_documents", sa.Integer(), nullable=False),
        sa.Column("total_records", sa.Integer(), nullable=False),
        sa.Column("cursor_document_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("parts", sa.JSON(), nullable=False),
        sa.Column("manifest_object_key", sa.String(1000), nullable=True),
        sa.Column("safe_error", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        tenant = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
        op.execute("ALTER TABLE data_export_jobs ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE data_export_jobs FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation ON data_export_jobs "
            f"USING (organization_id = {tenant}) WITH CHECK (organization_id = {tenant})"
        )


def downgrade() -> None:
    op.drop_table("data_export_jobs")
