"""Integrations, separated credentials, and mapping-profile versions
with RLS (EXP-001).

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


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
        "integrations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("integration_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("endpoint_url", sa.String(2000), nullable=True),
        sa.Column("credential_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("active_mapping_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "slug"),
    )
    op.create_table(
        "integration_credentials",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("integration_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mapping_profile_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("integration_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("target_schema", sa.JSON(), nullable=False),
        sa.Column("change_summary", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("integration_id", "version_number"),
    )
    op.create_index(
        "uq_mapping_profile_versions_single_published",
        "mapping_profile_versions",
        ["integration_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
        sqlite_where=sa.text("state = 'published'"),
    )
    for table in ("integrations", "integration_credentials", "mapping_profile_versions"):
        _rls(table)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("mapping_profile_versions", "integration_credentials", "integrations"):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index(
        "uq_mapping_profile_versions_single_published", table_name="mapping_profile_versions"
    )
    op.drop_table("mapping_profile_versions")
    op.drop_table("integration_credentials")
    op.drop_table("integrations")
