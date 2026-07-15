"""Persist exact catalog identities selected for order fields.

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "catalog_field_selections",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("field_key", sa.String(255), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("selection_source", sa.String(30), nullable=False),
        sa.Column("catalog_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("catalog_version_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("catalog_record_id", sa.Uuid(as_uuid=True), nullable=True, index=True),
        sa.Column("source_id", sa.String(200), nullable=True),
        sa.Column("display_name", sa.String(500), nullable=True),
        sa.Column("matched_value", sa.JSON(), nullable=True),
        sa.Column("value_fingerprint", sa.String(64), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("selected_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('selected', 'needs_review', 'confirmed_no_match', 'cleared')",
            name="ck_catalog_field_selections_status",
        ),
        sa.CheckConstraint(
            "selection_source IN ('machine', 'reviewer', 'correction')",
            name="ck_catalog_field_selections_source",
        ),
        sa.CheckConstraint(
            "(status = 'selected' AND catalog_record_id IS NOT NULL "
            "AND source_id IS NOT NULL AND display_name IS NOT NULL) OR "
            "(status <> 'selected' AND catalog_record_id IS NULL "
            "AND source_id IS NULL AND display_name IS NULL)",
            name="ck_catalog_field_selections_record_shape",
        ),
    )
    op.create_index(
        "ix_catalog_field_selections_run_field",
        "catalog_field_selections",
        ["run_id", "field_key", "row_index", "created_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE catalog_field_selections ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE catalog_field_selections FORCE ROW LEVEL SECURITY")
        op.execute(
            "CREATE POLICY tenant_isolation ON catalog_field_selections "
            f"USING (organization_id = {TENANT_MATCH}) "
            f"WITH CHECK (organization_id = {TENANT_MATCH})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON catalog_field_selections")
    op.drop_index("ix_catalog_field_selections_run_field", table_name="catalog_field_selections")
    op.drop_table("catalog_field_selections")
