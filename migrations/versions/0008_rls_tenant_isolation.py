"""Row-level security for high-risk tenant tables (PostgreSQL only).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-12

FORCE ROW LEVEL SECURITY means even the table owner is subject to the
policy. Rows are visible/writable only when the transaction has bound
``soa.organization_id`` to the matching tenant; unbound transactions see
nothing (fail closed).

service_credentials is deliberately NOT protected: API-key authentication
resolves credentials before tenant context exists (see soa_db.tenant_guard).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED_TABLES = ("workspaces", "memberships", "roles", "role_assignments")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (organization_id = current_setting('soa.organization_id', true)::uuid)
            WITH CHECK (organization_id = current_setting('soa.organization_id', true)::uuid)
            """
        )
    # Users may always see their OWN memberships (the /me listing spans
    # organizations); permissive policies OR together with tenant_isolation.
    op.execute(
        """
        CREATE POLICY user_self_access ON memberships
        USING (user_id = current_setting('soa.user_id', true)::uuid)
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP POLICY IF EXISTS user_self_access ON memberships")
    for table in PROTECTED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
