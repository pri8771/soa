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
# extended by later migrations). Deliberate exclusions:
# - service_credentials: API-key authentication must look up credentials
#   before any tenant context exists (random prefixes + one-way hashes).
# - users/organizations: identity/tenant ROOTS resolved at the auth boundary.
# - outbox_events, jobs: drained by the cross-tenant worker, which binds no
#   tenant GUC; organization_id there is payload, enforced at enqueue time.
# - audit_events: written from both tenant and system contexts (including
#   before a tenant is bound, e.g. org creation); tenant-facing audit reads
#   (future audit.read API) must scope through ScopedRepository queries.
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
    "policy_versions",  # 0015
    "artifacts",  # 0017
    "documents",  # 0018
    "upload_sessions",  # 0019
    "processing_runs",  # 0021
    "stage_runs",  # 0021
    "document_pages",  # 0022
    "extracted_fields",  # 0023
    "review_tasks",  # 0024
    "field_corrections",  # 0025
    "review_comments",  # 0026
    "canonical_payloads",  # 0028
    "integrations",  # 0029
    "integration_credentials",  # 0029
    "mapping_profile_versions",  # 0029
    "export_jobs",  # 0030
    "delivery_attempts",  # 0030
    "instruction_versions",  # 0031
    "gold_datasets",  # 0032
    "gold_dataset_versions",  # 0032
    "gold_documents",  # 0032
    "catalogs",  # 0033
    "catalog_versions",  # 0033
    "catalog_records",  # 0033
    "catalog_bindings",  # 0033
    "usage_ledger_entries",  # 0034
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
