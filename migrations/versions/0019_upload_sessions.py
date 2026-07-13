"""Upload-session table with RLS (ING-002).

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "upload_sessions",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("stream_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False, unique=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False),
        sa.Column("declared_filename", sa.String(255), nullable=False),
        sa.Column("declared_content_type", sa.String(100), nullable=False),
        sa.Column("declared_size_bytes", sa.Integer(), nullable=False),
        sa.Column("declared_sha256", sa.String(64), nullable=False),
        sa.Column("client_reference", sa.String(200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_upload_sessions_organization_id", "upload_sessions", ["organization_id"])
    op.create_index("ix_upload_sessions_stream_id", "upload_sessions", ["stream_id"])
    op.create_index("ix_upload_sessions_org_state", "upload_sessions", ["organization_id", "state"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE upload_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE upload_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON upload_sessions
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON upload_sessions")
    op.drop_index("ix_upload_sessions_org_state", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_stream_id", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_organization_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")
