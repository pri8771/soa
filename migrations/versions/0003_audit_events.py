"""Audit events table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("action", sa.String(200), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=False),
        sa.Column("target_id", sa.String(200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column(
            "summary",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_audit_events_org_occurred", "audit_events", ["organization_id", "occurred_at"]
    )
    op.create_index("ix_audit_events_target", "audit_events", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_target", table_name="audit_events")
    op.drop_index("ix_audit_events_org_occurred", table_name="audit_events")
    op.drop_table("audit_events")
