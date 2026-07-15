"""Tenant-managed provider credential metadata.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
RUNTIME_ROLE = "soa_app"


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("secret_reference", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_credentials_organization_id",
        "provider_credentials",
        ["organization_id"],
    )
    op.create_index(
        "ix_provider_credentials_org_provider",
        "provider_credentials",
        ["organization_id", "provider_name"],
    )
    op.create_index(
        "uq_provider_credentials_current",
        "provider_credentials",
        ["organization_id", "provider_name"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND revoked_at IS NULL"),
        sqlite_where=sa.text("superseded_at IS NULL AND revoked_at IS NULL"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE provider_credentials ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE provider_credentials FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON provider_credentials
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
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON provider_credentials TO {RUNTIME_ROLE}"
            )


def downgrade() -> None:
    op.drop_index("uq_provider_credentials_current", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_org_provider", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_organization_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
