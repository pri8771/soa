"""Harden RLS policies against empty-string GUCs (PostgreSQL only).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12

``set_config(name, value, true)`` reverts at transaction end, but on a
pooled connection the custom GUC then reads as '' (empty string) rather
than NULL for the rest of the session. The 0008 policies cast the setting
straight to uuid, so an unbound transaction on a previously-bound
connection errored with "invalid input syntax for type uuid" instead of
cleanly returning zero rows. NULLIF makes both unbound shapes ('' and
NULL) evaluate to NULL: policy false, fail closed, no error.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED_TABLES = ("workspaces", "memberships", "roles", "role_assignments")

TENANT_MATCH = "NULLIF(current_setting('soa.organization_id', true), '')::uuid"
USER_MATCH = "NULLIF(current_setting('soa.user_id', true), '')::uuid"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in PROTECTED_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (organization_id = {TENANT_MATCH})
            WITH CHECK (organization_id = {TENANT_MATCH})
            """
        )
    op.execute("DROP POLICY user_self_access ON memberships")
    op.execute(
        f"""
        CREATE POLICY user_self_access ON memberships
        USING (user_id = {USER_MATCH})
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP POLICY user_self_access ON memberships")
    op.execute(
        """
        CREATE POLICY user_self_access ON memberships
        USING (user_id = current_setting('soa.user_id', true)::uuid)
        """
    )
    for table in PROTECTED_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (organization_id = current_setting('soa.organization_id', true)::uuid)
            WITH CHECK (organization_id = current_setting('soa.organization_id', true)::uuid)
            """
        )
