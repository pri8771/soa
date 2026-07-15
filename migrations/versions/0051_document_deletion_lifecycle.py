"""Durable document-deletion requests and legal holds.

Revision ID: 0051
Revises: 0050
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
RUNTIME_ROLE = "soa_app"


def _tenant_table(table: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
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
    role_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": RUNTIME_ROLE},
    ).scalar()
    if role_exists is not None:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RUNTIME_ROLE}")


def upgrade() -> None:
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending_approval"),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("requested_by", sa.String(200), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(200), nullable=True),
        sa.Column("approval_reason", sa.String(500), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(200), nullable=True),
        sa.Column("cancellation_reason", sa.String(500), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error", sa.String(500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending_approval','approved','running','failed','completed','cancelled')",
            name="deletion_requests_state_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deletion_requests_organization_id",
        "deletion_requests",
        ["organization_id"],
    )
    op.create_index("ix_deletion_requests_document_id", "deletion_requests", ["document_id"])
    op.create_index(
        "uq_deletion_requests_org_document",
        "deletion_requests",
        ["organization_id", "document_id"],
        unique=True,
    )

    op.create_table(
        "legal_holds",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("placed_by", sa.String(200), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.String(200), nullable=True),
        sa.Column("release_reason", sa.String(500), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('active','released')", name="legal_holds_state_valid"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_legal_holds_organization_id", "legal_holds", ["organization_id"])
    op.create_index("ix_legal_holds_document_id", "legal_holds", ["document_id"])
    op.create_index(
        "uq_legal_holds_one_active_per_document",
        "legal_holds",
        ["organization_id", "document_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )

    _tenant_table("deletion_requests")
    _tenant_table("legal_holds")


def downgrade() -> None:
    op.drop_index("uq_legal_holds_one_active_per_document", table_name="legal_holds")
    op.drop_index("ix_legal_holds_document_id", table_name="legal_holds")
    op.drop_index("ix_legal_holds_organization_id", table_name="legal_holds")
    op.drop_table("legal_holds")
    op.drop_index("uq_deletion_requests_org_document", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_document_id", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_organization_id", table_name="deletion_requests")
    op.drop_table("deletion_requests")
