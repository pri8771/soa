"""Feature flags and quotas with RLS (ANA-009).

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("stream_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("limit_value", sa.Integer(), nullable=True),
        sa.Column("limit_unit", sa.String(30), nullable=True),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("review_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "key", "stream_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE feature_flags ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE feature_flags FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON feature_flags
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )


def downgrade() -> None:
    op.drop_table("feature_flags")
