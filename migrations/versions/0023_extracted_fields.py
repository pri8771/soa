"""Extracted-field table with RLS (PRC-007).

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "extracted_fields",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False),
        sa.Column("run_id", _guid(), nullable=False),
        sa.Column("field_key", sa.String(255), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("normalized_value", sa.JSON(), nullable=True),
        sa.Column("normalization_error", sa.String(500), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_model", sa.String(100), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("validation_status", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_extracted_fields_organization_id", "extracted_fields", ["organization_id"])
    op.create_index("ix_extracted_fields_document_id", "extracted_fields", ["document_id"])
    op.create_index("ix_extracted_fields_run_id", "extracted_fields", ["run_id"])
    op.create_index(
        "uq_extracted_fields_run_key_row",
        "extracted_fields",
        ["run_id", "field_key", sa.literal_column("coalesce(row_index, -1)")],
        unique=True,
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE extracted_fields ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE extracted_fields FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON extracted_fields
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON extracted_fields")
    op.drop_index("uq_extracted_fields_run_key_row", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_run_id", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_document_id", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_organization_id", table_name="extracted_fields")
    op.drop_table("extracted_fields")
