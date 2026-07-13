"""Gold evaluation datasets with RLS (AIO-015).

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"

TABLES = ("gold_datasets", "gold_dataset_versions", "gold_documents")


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
        "gold_datasets",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("privacy_classification", sa.String(50), nullable=False),
        sa.Column("sharing_agreement_ref", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "slug"),
    )
    op.create_table(
        "gold_dataset_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("change_summary", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id", "version_number"),
    )
    op.create_index(
        "uq_gold_dataset_versions_single_published",
        "gold_dataset_versions",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("state = 'published'"),
        sqlite_where=sa.text("state = 'published'"),
    )
    op.create_table(
        "gold_documents",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("dataset_version_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("source_document_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("split", sa.String(20), nullable=False),
        sa.Column("expected_class", sa.String(100), nullable=True),
        sa.Column("ground_truth", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_version_id", "document_sha256"),
    )
    for table in TABLES:
        _rls(table)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in reversed(TABLES):
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("gold_documents")
    op.drop_index("uq_gold_dataset_versions_single_published", table_name="gold_dataset_versions")
    op.drop_table("gold_dataset_versions")
    op.drop_table("gold_datasets")
