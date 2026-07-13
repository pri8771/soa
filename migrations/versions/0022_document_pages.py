"""Document-page table with RLS (PRC-005).

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "document_pages",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=False),
        sa.Column("document_id", _guid(), nullable=False),
        sa.Column("run_id", _guid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("dpi", sa.Integer(), nullable=True),
        sa.Column("image_artifact_id", _guid(), nullable=False),
        sa.Column("text_artifact_id", _guid(), nullable=True),
        sa.Column("layout_artifact_id", _guid(), nullable=True),
        sa.Column("rotation_degrees", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "page_number", name="uq_document_pages_run_page"),
    )
    op.create_index("ix_document_pages_organization_id", "document_pages", ["organization_id"])
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])
    op.create_index("ix_document_pages_run_id", "document_pages", ["run_id"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE document_pages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_pages FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON document_pages
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON document_pages")
    op.drop_index("ix_document_pages_run_id", table_name="document_pages")
    op.drop_index("ix_document_pages_document_id", table_name="document_pages")
    op.drop_index("ix_document_pages_organization_id", table_name="document_pages")
    op.drop_table("document_pages")
