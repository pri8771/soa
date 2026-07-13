"""Catalogs, versions, records, and stream bindings with RLS (CAT-001).

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"

TABLES = ("catalogs", "catalog_versions", "catalog_records", "catalog_bindings")


def _rls(table: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
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


def upgrade() -> None:
    op.create_table(
        "catalogs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("catalog_type", sa.String(50), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("active_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "slug"),
    )
    op.create_table(
        "catalog_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("catalog_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("change_summary", sa.String(500), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("catalog_id", "version_number"),
    )
    op.create_index(
        "uq_catalog_versions_single_active",
        "catalog_versions",
        ["catalog_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
        sqlite_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "catalog_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("catalog_version_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("catalog_version_id", "source_id"),
    )
    op.create_table(
        "catalog_bindings",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("stream_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("catalog_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("pinned_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("stream_id", "catalog_id"),
    )
    for table in TABLES:
        _rls(table)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in reversed(TABLES):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("catalog_bindings")
    op.drop_table("catalog_records")
    op.drop_index("uq_catalog_versions_single_active", table_name="catalog_versions")
    op.drop_table("catalog_versions")
    op.drop_table("catalogs")
