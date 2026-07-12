"""Transactional outbox table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("organization_id", _guid(), nullable=True),
        sa.Column("event_type", sa.String(200), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(300), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_events_dedupe_key"),
    )
    op.create_index("ix_outbox_events_organization_id", "outbox_events", ["organization_id"])
    op.create_index("ix_outbox_events_claim", "outbox_events", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_claim", table_name="outbox_events")
    op.drop_index("ix_outbox_events_organization_id", table_name="outbox_events")
    op.drop_table("outbox_events")
