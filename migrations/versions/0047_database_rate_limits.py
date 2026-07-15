"""Durable cross-replica abuse-control windows.

Revision ID: 0047
Revises: 0046

These are intentionally GLOBAL tables without tenant RLS. Rate limiting must
run before tenant resolution on some surfaces, and the only identity material
stored is a keyed HMAC digest. Tenant RLS would make one atomic cross-replica
budget impossible and would not protect additional customer data because no
raw identity or business payload is present.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "soa_app"


def upgrade() -> None:
    json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    op.create_table(
        "rate_limit_buckets",
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("events", json_type, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(operation) BETWEEN 1 AND 100", name="operation_length"),
        sa.CheckConstraint("length(identity_hash) = 64", name="identity_hash_length"),
        sa.PrimaryKeyConstraint("operation", "identity_hash"),
    )
    op.create_index(
        "ix_rate_limit_buckets_expires_at",
        "rate_limit_buckets",
        ["expires_at"],
    )
    op.create_table(
        "rate_limit_counters",
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("shard", sa.SmallInteger(), nullable=False),
        sa.Column("allowed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("denied", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(operation) BETWEEN 1 AND 100", name="operation_length"),
        sa.CheckConstraint("shard BETWEEN 0 AND 255", name="shard_range"),
        sa.CheckConstraint("allowed >= 0", name="allowed_nonnegative"),
        sa.CheckConstraint("denied >= 0", name="denied_nonnegative"),
        sa.PrimaryKeyConstraint("operation", "shard"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "COMMENT ON TABLE rate_limit_buckets IS "
            "'Global abuse-control state: keyed identity digests only; deliberately no tenant RLS'"
        )
        op.execute(
            "COMMENT ON TABLE rate_limit_counters IS "
            "'Global identity-free 256-way sharded abuse counters; deliberately no tenant RLS'"
        )
        role_exists = bind.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
            {"role": RUNTIME_ROLE},
        ).scalar()
        if role_exists is not None:
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
                f"rate_limit_buckets, rate_limit_counters TO {RUNTIME_ROLE}"
            )


def downgrade() -> None:
    op.drop_table("rate_limit_counters")
    op.drop_index("ix_rate_limit_buckets_expires_at", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
