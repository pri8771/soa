"""Persist bounded cleanup retries for rolled-back external resources.

Revision ID: 0053
Revises: 0052
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
RUNTIME_ROLE = "soa_app"


def upgrade() -> None:
    op.create_table(
        "external_cleanup_intents",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(20), nullable=False),
        # An opaque object key or secret reference only. Secret values and
        # object bytes are never stored here or copied into queue payloads.
        sa.Column("resource_locator", sa.Text(), nullable=True),
        sa.Column("locator_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_reconciled_job_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('object','secret')",
            name="external_cleanup_resource_type_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','completed','manual_intervention')",
            name="external_cleanup_state_valid",
        ),
        sa.CheckConstraint(
            "state = 'completed' OR resource_locator IS NOT NULL",
            name="external_cleanup_live_locator_present",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="external_cleanup_attempts_non_negative",
        ),
        sa.CheckConstraint(
            "dispatch_count >= 1 AND dispatch_count <= 3",
            name="external_cleanup_dispatch_count_bounded",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "resource_type",
            "locator_sha256",
            name="uq_external_cleanup_resource",
        ),
    )
    op.create_index(
        "ix_external_cleanup_intents_organization_id",
        "external_cleanup_intents",
        ["organization_id"],
    )
    op.create_index(
        "ix_external_cleanup_org_state",
        "external_cleanup_intents",
        ["organization_id", "state"],
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE external_cleanup_intents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_cleanup_intents FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON external_cleanup_intents
        USING (organization_id = {TENANT_MATCH})
        WITH CHECK (organization_id = {TENANT_MATCH})
        """
    )
    role_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": RUNTIME_ROLE},
    ).scalar()
    if role_exists is not None:
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON external_cleanup_intents TO soa_app")


def downgrade() -> None:
    op.drop_index("ix_external_cleanup_org_state", table_name="external_cleanup_intents")
    op.drop_index(
        "ix_external_cleanup_intents_organization_id",
        table_name="external_cleanup_intents",
    )
    op.drop_table("external_cleanup_intents")
