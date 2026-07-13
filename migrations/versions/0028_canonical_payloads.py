"""Immutable canonical payloads with RLS (CAN-003).

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "canonical_payloads",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "schema_version"),
    )
    op.create_index(
        "ix_canonical_payloads_organization_id", "canonical_payloads", ["organization_id"]
    )
    op.create_index("ix_canonical_payloads_document_id", "canonical_payloads", ["document_id"])
    op.create_index("ix_canonical_payloads_run_id", "canonical_payloads", ["run_id"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE canonical_payloads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE canonical_payloads FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON canonical_payloads
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON canonical_payloads")
    op.drop_index("ix_canonical_payloads_run_id", table_name="canonical_payloads")
    op.drop_index("ix_canonical_payloads_document_id", table_name="canonical_payloads")
    op.drop_index("ix_canonical_payloads_organization_id", table_name="canonical_payloads")
    op.drop_table("canonical_payloads")
