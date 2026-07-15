"""Reduce the runtime role to DML without transferring schema ownership.

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-15

This migration is a PostgreSQL deployment contract, not an application-table
change. It intentionally has no destructive downgrade: revoking runtime access
during a code rollback would take the API and worker offline.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "soa_app"
CLOUD_SQL_ADMIN_ROLE = "cloudsqlsuperuser"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    role_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": RUNTIME_ROLE},
    ).scalar()
    if role_exists is None:
        # Portable CI and developer databases may intentionally omit the
        # production role. Production IaC creates it before the migrator runs.
        return
    # Cloud SQL automatically grants every newly-created built-in PostgreSQL
    # user its broad ``cloudsqlsuperuser`` role unless custom roles already
    # exist. Terraform must create the login before Alembic can grant table
    # access, so the first privileged migration removes that bootstrap grant.
    # The runtime then has no role attributes capable of bypassing the DML/RLS
    # boundary below. Local PostgreSQL does not normally define this role.
    cloud_sql_role_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": CLOUD_SQL_ADMIN_ROLE},
    ).scalar()
    if cloud_sql_role_exists is not None:
        op.execute(f"REVOKE {CLOUD_SQL_ADMIN_ROLE} FROM {RUNTIME_ROLE}")
    op.execute(
        f"ALTER ROLE {RUNTIME_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    # Access grants remain valid across schema/code rollback. See module note.
    pass
