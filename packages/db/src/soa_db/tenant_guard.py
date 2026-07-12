"""Database-level tenant binding for RLS defense in depth (TEN-010).

On PostgreSQL, high-risk tenant tables carry FORCED row-level-security
policies keyed to the ``soa.organization_id`` transaction setting. Binding
happens once per authorized unit of work; a transaction that never binds a
tenant reads and writes NOTHING from those tables — accidental unscoped
queries fail closed at the database itself, beneath the repository layer.

On non-PostgreSQL engines (SQLite tests) binding is a no-op; the repository
layer remains the enforced boundary there.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TENANT_GUC = "soa.organization_id"
USER_GUC = "soa.user_id"

# Tables protected by RLS policies (kept in sync with migration 0008 and
# extended by later migrations; service_credentials is deliberately excluded
# because API-key authentication must look up credentials before any tenant
# context exists — it is protected by random prefixes and one-way hashes).
RLS_PROTECTED_TABLES = (
    "workspaces",
    "memberships",
    "roles",
    "role_assignments",
    "processes",  # 0011
    "process_versions",  # 0011
    "streams",  # 0012
    "stream_versions",  # 0012
    "schema_versions",  # 0013
    "rule_set_versions",  # 0014
)


async def bind_tenant(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Bind the transaction to a tenant. Call after authorization succeeds."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config(:guc, :value, true)"),
        {"guc": TENANT_GUC, "value": str(organization_id)},
    )


async def bind_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Bind the transaction to a user identity for self-scoped reads (a user
    listing their OWN memberships across organizations)."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config(:guc, :value, true)"),
        {"guc": USER_GUC, "value": str(user_id)},
    )
