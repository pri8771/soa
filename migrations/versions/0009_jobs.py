"""Durable jobs table (JOB-001, DELIVERY_PLAN §5.5).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

Deliberately NOT under row-level security: the worker is a cross-tenant
system actor and binds no tenant GUC (same reasoning as outbox_events).
Tenant scoping for user-facing job reads happens at the API layer.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _guid() -> sa.types.TypeEngine[object]:
    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", _guid(), primary_key=True),
        sa.Column("job_type", sa.String(200), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("organization_id", _guid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(300), nullable=True),
        sa.Column("lock_owner", sa.String(100), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'dead_letter', 'cancelled')",
            name="ck_jobs_jobs_status_valid",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_jobs_attempts_non_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs_jobs_max_attempts_positive"),
    )
    op.create_index("ix_jobs_organization_id", "jobs", ["organization_id"])
    op.create_index("ix_jobs_claim", "jobs", ["status", "priority", "run_after"])


def downgrade() -> None:
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("ix_jobs_organization_id", table_name="jobs")
    op.drop_table("jobs")
