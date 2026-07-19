"""Per-tenant outbox delivery destination (EXP-012).

Outbox events published for exactly one GLOBAL URL for every organization;
this table lets an org admin route their own events to their own receiver.
No signing-secret column — the global outbox_signing_secret continues to
sign every delivery in this iteration.

Revision ID: 0057
Revises: 0056
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "outbox_destinations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("destination_url", sa.String(2000), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE outbox_destinations ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE outbox_destinations FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON outbox_destinations
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON outbox_destinations")
    op.drop_table("outbox_destinations")
