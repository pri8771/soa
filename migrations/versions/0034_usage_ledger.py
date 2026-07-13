"""Cost and usage ledger with RLS (ANA-003).

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "usage_ledger_entries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("stream_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_model", sa.String(100), nullable=True),
        sa.Column("cost_category", sa.String(30), nullable=False),
        sa.Column("billed_unit", sa.String(30), nullable=True),
        sa.Column("billed_quantity", sa.BigInteger(), nullable=True),
        sa.Column("estimated_cost_cents", sa.Integer(), nullable=False),
        sa.Column("adjustment_cents", sa.Integer(), nullable=False),
        sa.Column("adjusts_entry_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("source_reference", sa.String(200), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "source_reference"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE usage_ledger_entries ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE usage_ledger_entries FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON usage_ledger_entries
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )


def downgrade() -> None:
    op.drop_table("usage_ledger_entries")
