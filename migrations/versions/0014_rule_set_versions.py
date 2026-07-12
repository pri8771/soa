"""Rule-set-version table with RLS (CFG-004).

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "rule_set_versions",
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
        sa.UniqueConstraint("process_id", "version_number", name="uq_rule_set_versions_process_id"),
    )
    op.create_index(
        "ix_rule_set_versions_organization_id", "rule_set_versions", ["organization_id"]
    )
    op.create_index("ix_rule_set_versions_process_id", "rule_set_versions", ["process_id"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE rule_set_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rule_set_versions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON rule_set_versions
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON rule_set_versions")
    op.drop_index("ix_rule_set_versions_process_id", table_name="rule_set_versions")
    op.drop_index("ix_rule_set_versions_organization_id", table_name="rule_set_versions")
    op.drop_table("rule_set_versions")
