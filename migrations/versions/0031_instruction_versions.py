"""Versioned extraction instructions with RLS (AIO-010).

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "instruction_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("stream_version_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("schema_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("change_summary", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "stream_version_id", "version_number"),
    )
    op.create_index(
        "uq_instruction_versions_single_published",
        "instruction_versions",
        ["organization_id", "stream_version_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
        sqlite_where=sa.text("state = 'published'"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE instruction_versions ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE instruction_versions FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON instruction_versions
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON instruction_versions")
    op.drop_index("uq_instruction_versions_single_published", table_name="instruction_versions")
    op.drop_table("instruction_versions")
