"""Stream and stream-version tables with RLS (CFG-002).

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
RLS_TABLES = ("streams", "stream_versions")


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def _json() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "streams",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("process_id", _guid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("active_version_id", _guid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "slug", name="uq_streams_organization_id"),
    )
    op.create_index("ix_streams_organization_id", "streams", ["organization_id"])
    op.create_index("ix_streams_process_id", "streams", ["process_id"])

    op.create_table(
        "stream_versions",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("stream_id", _guid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("overrides", _json(), nullable=False),
        sa.Column("resolved_snapshot", _json(), nullable=True),
        sa.Column("pinned_process_version_id", _guid(), nullable=True),
        sa.Column("change_summary", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("stream_id", "version_number", name="uq_stream_versions_stream_id"),
    )
    op.create_index("ix_stream_versions_organization_id", "stream_versions", ["organization_id"])
    op.create_index("ix_stream_versions_stream_id", "stream_versions", ["stream_id"])

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
    op.drop_index("ix_stream_versions_stream_id", table_name="stream_versions")
    op.drop_index("ix_stream_versions_organization_id", table_name="stream_versions")
    op.drop_table("stream_versions")
    op.drop_index("ix_streams_process_id", table_name="streams")
    op.drop_index("ix_streams_organization_id", table_name="streams")
    op.drop_table("streams")
