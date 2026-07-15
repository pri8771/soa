"""Durable tenant-scoped provider health and routing metrics.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
RUNTIME_ROLE = "soa_app"


def upgrade() -> None:
    op.create_table(
        "provider_runtime_metrics",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fallback_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_latency_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("quality_sum_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("quality_sample_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_class", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint("success_count >= 0", name="success_count_nonnegative"),
        sa.CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
        sa.CheckConstraint("fallback_count >= 0", name="fallback_count_nonnegative"),
        sa.CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_nonnegative"),
        sa.CheckConstraint("total_latency_ms >= 0", name="total_latency_nonnegative"),
        sa.CheckConstraint("total_cost_cents >= 0", name="total_cost_nonnegative"),
        sa.CheckConstraint("quality_sum_micros >= 0", name="quality_sum_nonnegative"),
        sa.CheckConstraint("quality_sample_count >= 0", name="quality_samples_nonnegative"),
        sa.CheckConstraint(
            "last_failure_class IS NULL OR last_failure_class IN ('retryable', 'terminal')",
            name="failure_class_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "capability", "provider"),
    )
    op.create_index(
        "ix_provider_runtime_metrics_organization_id",
        "provider_runtime_metrics",
        ["organization_id"],
    )
    op.create_index(
        "ix_provider_runtime_metrics_org_capability",
        "provider_runtime_metrics",
        ["organization_id", "capability"],
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE provider_runtime_metrics ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE provider_runtime_metrics FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON provider_runtime_metrics
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )
        role_exists = bind.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
            {"role": RUNTIME_ROLE},
        ).scalar()
        if role_exists is not None:
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON provider_runtime_metrics "
                f"TO {RUNTIME_ROLE}"
            )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_runtime_metrics_org_capability",
        table_name="provider_runtime_metrics",
    )
    op.drop_index(
        "ix_provider_runtime_metrics_organization_id",
        table_name="provider_runtime_metrics",
    )
    op.drop_table("provider_runtime_metrics")
