"""Documents table with RLS and idempotency index (ING-001).

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("stream_id", _guid(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("source_channel", sa.String(20), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("client_reference", sa.String(200), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_metadata",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("state_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_documents_organization_id", "documents", ["organization_id"])
    op.create_index("ix_documents_stream_id", "documents", ["stream_id"])
    op.create_index("ix_documents_org_sha256", "documents", ["organization_id", "content_sha256"])
    op.create_index("ix_documents_org_state", "documents", ["organization_id", "state"])
    op.create_index(
        "uq_documents_client_reference",
        "documents",
        ["organization_id", "stream_id", "client_reference"],
        unique=True,
        postgresql_where=sa.text("client_reference IS NOT NULL"),
        sqlite_where=sa.text("client_reference IS NOT NULL"),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON documents
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON documents")
    for index in (
        "uq_documents_client_reference",
        "ix_documents_org_state",
        "ix_documents_org_sha256",
        "ix_documents_stream_id",
        "ix_documents_organization_id",
    ):
        op.drop_index(index, table_name="documents")
    op.drop_table("documents")
