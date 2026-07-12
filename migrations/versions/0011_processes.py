"""Process and process-version tables with RLS (CFG-001).

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
RLS_TABLES = ("processes", "process_versions")


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "processes",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("active_version_id", _guid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "slug", name="uq_processes_organization_id"),
    )
    op.create_index("ix_processes_organization_id", "processes", ["organization_id"])

    op.create_table(
        "process_versions",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("process_id", _guid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "definition",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("change_summary", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("process_id", "version_number", name="uq_process_versions_process_id"),
    )
    op.create_index("ix_process_versions_organization_id", "process_versions", ["organization_id"])
    op.create_index("ix_process_versions_process_id", "process_versions", ["process_id"])

    if op.get_bind().dialect.name != "postgresql":
        return
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_process_versions_process_id", table_name="process_versions")
    op.drop_index("ix_process_versions_organization_id", table_name="process_versions")
    op.drop_table("process_versions")
    op.drop_index("ix_processes_organization_id", table_name="processes")
    op.drop_table("processes")
