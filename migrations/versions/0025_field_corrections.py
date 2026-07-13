"""Append-only field corrections with RLS (REV-009).

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "field_corrections",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False),
        sa.Column("run_id", _guid(), nullable=False),
        sa.Column("task_id", _guid(), nullable=False),
        sa.Column("field_key", sa.String(255), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("previous_raw_value", sa.Text(), nullable=True),
        sa.Column("corrected_raw_value", sa.Text(), nullable=True),
        sa.Column("corrected_normalized_value", sa.JSON(), nullable=True),
        sa.Column("normalization_error", sa.String(500), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("evidence_selection", sa.JSON(), nullable=True),
        sa.Column("corrected_by", sa.String(200), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_field_corrections_organization_id", "field_corrections", ["organization_id"]
    )
    op.create_index("ix_field_corrections_document_id", "field_corrections", ["document_id"])
    op.create_index("ix_field_corrections_run_id", "field_corrections", ["run_id"])
    op.create_index("ix_field_corrections_task_id", "field_corrections", ["task_id"])
    op.create_index("ix_field_corrections_run_field", "field_corrections", ["run_id", "field_key"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE field_corrections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE field_corrections FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON field_corrections
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON field_corrections")
    op.drop_index("ix_field_corrections_run_field", table_name="field_corrections")
    op.drop_index("ix_field_corrections_task_id", table_name="field_corrections")
    op.drop_index("ix_field_corrections_run_id", table_name="field_corrections")
    op.drop_index("ix_field_corrections_document_id", table_name="field_corrections")
    op.drop_index("ix_field_corrections_organization_id", table_name="field_corrections")
    op.drop_table("field_corrections")
