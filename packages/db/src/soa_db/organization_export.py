"""Allowlisted, resumable tenant-record collection for organization exports.

The worker deliberately does not depend on ``soa_api``.  Several control-plane
tables are owned by that package, however, so this module reflects only the
closed table allowlist below and applies an explicit tenant predicate to every
query.  Reflection is used for schema compatibility, never for discovery.

Each call reads at most ``limit`` records from one category.  The durable export
job stores the returned cursor and completion marker alongside its part
metadata, allowing a retry to overwrite the same deterministic object key.
"""

from __future__ import annotations

import base64
import uuid
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

ScopeKind = Literal["organization_row", "organization_id", "membership_users"]


@dataclass(frozen=True)
class OrganizationExportCategory:
    """One explicitly permitted tenant table and its scoping rule."""

    key: str
    table_name: str
    description: str
    scope: ScopeKind = "organization_id"
    snapshot_column: str = "created_at"


# This is a security boundary, not a table-discovery convenience.  A newly
# created table is excluded until its tenant semantics and secret posture are
# reviewed and an entry is deliberately added here.
ORGANIZATION_EXPORT_CATEGORIES: tuple[OrganizationExportCategory, ...] = (
    OrganizationExportCategory(
        "organization",
        "organizations",
        "Organization profile and lifecycle state.",
        "organization_row",
    ),
    OrganizationExportCategory("workspaces", "workspaces", "Organization workspaces."),
    OrganizationExportCategory(
        "users",
        "users",
        "User identities linked to the organization through memberships.",
        "membership_users",
    ),
    OrganizationExportCategory("memberships", "memberships", "Memberships and invitations."),
    OrganizationExportCategory("roles", "roles", "Organization RBAC roles."),
    OrganizationExportCategory(
        "role_assignments", "role_assignments", "Membership-to-role assignments."
    ),
    OrganizationExportCategory("processes", "processes", "Document processes."),
    OrganizationExportCategory(
        "process_versions", "process_versions", "Immutable process configuration versions."
    ),
    OrganizationExportCategory("streams", "streams", "Input streams."),
    OrganizationExportCategory(
        "stream_versions", "stream_versions", "Immutable stream configuration versions."
    ),
    OrganizationExportCategory(
        "schema_versions", "schema_versions", "Immutable extraction schema versions."
    ),
    OrganizationExportCategory(
        "rule_set_versions", "rule_set_versions", "Immutable validation rule-set versions."
    ),
    OrganizationExportCategory(
        "policy_versions", "policy_versions", "Immutable organization policy versions."
    ),
    OrganizationExportCategory(
        "service_credentials",
        "service_credentials",
        "Service credential metadata and one-way key digests; never plaintext keys.",
    ),
    OrganizationExportCategory(
        "upload_sessions", "upload_sessions", "Upload-session metadata and object references."
    ),
    OrganizationExportCategory("catalogs", "catalogs", "Tenant catalogs."),
    OrganizationExportCategory(
        "catalog_versions", "catalog_versions", "Immutable catalog versions."
    ),
    OrganizationExportCategory("catalog_records", "catalog_records", "Catalog records."),
    OrganizationExportCategory(
        "catalog_bindings", "catalog_bindings", "Stream-to-catalog bindings."
    ),
    OrganizationExportCategory(
        "instruction_versions", "instruction_versions", "Immutable extraction instructions."
    ),
    OrganizationExportCategory("integrations", "integrations", "Outbound integrations."),
    OrganizationExportCategory(
        "integration_credentials",
        "integration_credentials",
        "Integration credential metadata and opaque secret-store references.",
    ),
    OrganizationExportCategory(
        "provider_credentials",
        "provider_credentials",
        "Provider credential metadata and opaque secret-store references.",
    ),
    OrganizationExportCategory(
        "external_cleanup_intents",
        "external_cleanup_intents",
        "Bounded external-resource cleanup status and opaque references.",
    ),
    OrganizationExportCategory(
        "mapping_profile_versions",
        "mapping_profile_versions",
        "Immutable integration mapping profiles.",
    ),
    OrganizationExportCategory("feature_flags", "feature_flags", "Tenant feature controls."),
    OrganizationExportCategory("gold_datasets", "gold_datasets", "Gold evaluation datasets."),
    OrganizationExportCategory(
        "gold_dataset_versions", "gold_dataset_versions", "Immutable gold dataset versions."
    ),
    OrganizationExportCategory(
        "gold_documents", "gold_documents", "Gold dataset examples and ground truth."
    ),
    OrganizationExportCategory(
        "evaluation_runs", "evaluation_runs", "Evaluation runs, checkpoints, and reports."
    ),
    OrganizationExportCategory(
        "usage_ledger_entries", "usage_ledger_entries", "Usage and billing ledger entries."
    ),
    OrganizationExportCategory(
        "provider_runtime_metrics",
        "provider_runtime_metrics",
        "Provider health, routing, latency, fallback, and cost aggregates.",
    ),
    OrganizationExportCategory("jobs", "jobs", "Tenant background-job operational records."),
    OrganizationExportCategory(
        "outbox_events", "outbox_events", "Tenant event-delivery outbox records."
    ),
    OrganizationExportCategory(
        "deletion_tombstones", "deletion_tombstones", "Data-deletion workflow evidence."
    ),
    OrganizationExportCategory(
        "deletion_requests", "deletion_requests", "Data-deletion request and approval history."
    ),
    OrganizationExportCategory(
        "legal_holds", "legal_holds", "Document legal-hold and release history."
    ),
    OrganizationExportCategory(
        "data_export_jobs", "data_export_jobs", "Prior durable data-export metadata."
    ),
    OrganizationExportCategory(
        "audit_events",
        "audit_events",
        "Organization audit trail.",
        snapshot_column="occurred_at",
    ),
)


@dataclass(frozen=True)
class OrganizationExportPage:
    category: OrganizationExportCategory
    records: list[dict[str, Any]]
    last_id: str | None
    complete: bool


_SAFE_SENSITIVE_SUFFIXES = (
    "_algorithm",
    "_count",
    "_digest",
    "_enabled",
    "_hash",
    "_id",
    "_kind",
    "_limit",
    "_method",
    "_name",
    "_policy",
    "_prefix",
    "_ref",
    "_reference",
    "_status",
    "_type",
    "_url",
    "_usage",
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "hmac",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "token",
)
REDACTED = "[redacted]"

# Reflected schemas are immutable for a worker deployment. Cache them per
# SQLAlchemy engine so a large export pays information-schema reflection once,
# not once for every category page. Weak keys avoid retaining test/ephemeral
# engines after disposal.
_REFLECTION_CACHE: weakref.WeakKeyDictionary[object, dict[str, Table]] = weakref.WeakKeyDictionary()


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized.endswith(_SAFE_SENSITIVE_SUFFIXES):
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_export_value(value: Any, *, key: str | None = None) -> Any:
    """Convert a database value to JSON while removing plaintext secrets.

    Opaque references, identifiers, prefixes, and one-way hashes/digests are
    intentionally retained because they are customer-owned metadata useful for
    portability and incident investigation.
    """

    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_export_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [sanitize_export_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(value).decode("ascii")
    return value


async def _reflect_tables(session: AsyncSession, names: set[str]) -> dict[str, Table]:
    engine = session.get_bind()
    cached = _REFLECTION_CACHE.get(engine)
    if cached is not None:
        return {name: cached[name] for name in names if name in cached}

    def reflect(sync_session: Any) -> dict[str, Table]:
        metadata = MetaData()
        connection = sync_session.connection()
        available = set(inspect(connection).get_table_names())
        allowed_names = {category.table_name for category in ORGANIZATION_EXPORT_CATEGORIES}
        allowed_names.add("memberships")
        return {
            name: Table(name, metadata, autoload_with=connection)
            for name in allowed_names
            if name in available
        }

    cached = await session.run_sync(reflect)
    _REFLECTION_CACHE[engine] = cached
    return {name: cached[name] for name in names if name in cached}


def _uuid_parameter(session: AsyncSession, value: uuid.UUID) -> uuid.UUID | str:
    bind = session.get_bind()
    return value.hex if bind.dialect.name == "sqlite" else value


async def collect_organization_export_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    snapshot_at: datetime,
    category: OrganizationExportCategory,
    after_id: str | None = None,
    limit: int = 250,
) -> OrganizationExportPage:
    """Collect one bounded, tenant-scoped page from an allowlisted category.

    Missing tables and required tenant/snapshot columns fail closed.  An
    incomplete deployment must never produce a successful-looking export that
    silently omitted a category; arbitrary tables are never discovered or
    exported.
    """

    if category not in ORGANIZATION_EXPORT_CATEGORIES:
        raise ValueError("organization export category is not allowlisted")
    if limit < 1 or limit > 1000:
        raise ValueError("organization export page limit must be between 1 and 1000")

    names = {category.table_name}
    if category.scope == "membership_users":
        names.add("memberships")
    tables = await _reflect_tables(session, names)
    table = tables.get(category.table_name)
    if table is None:
        raise ValueError(f"allowlisted export table {category.table_name!r} does not exist")
    if "id" not in table.c:
        raise ValueError(f"allowlisted export table {category.table_name!r} has no id column")

    organization_value = _uuid_parameter(session, organization_id)
    if category.scope == "organization_row":
        statement = select(table).where(table.c.id == organization_value)
    elif category.scope == "membership_users":
        memberships = tables.get("memberships")
        if memberships is None:
            raise ValueError("allowlisted export table 'memberships' does not exist")
        if "created_at" not in memberships.c:
            raise ValueError("allowlisted export table 'memberships' has no 'created_at' column")
        statement = (
            select(*table.c)
            .select_from(table.join(memberships, memberships.c.user_id == table.c.id))
            .where(
                memberships.c.organization_id == organization_value,
                memberships.c.created_at <= snapshot_at,
            )
            .distinct()
        )
    else:
        if "organization_id" not in table.c:
            raise ValueError(
                f"allowlisted export table {category.table_name!r} has no organization_id"
            )
        statement = select(table).where(table.c.organization_id == organization_value)

    if category.snapshot_column in table.c:
        statement = statement.where(table.c[category.snapshot_column] <= snapshot_at)
    else:
        raise ValueError(
            f"allowlisted export table {category.table_name!r} has no "
            f"{category.snapshot_column!r} snapshot column"
        )
    # Direct document-target audit events are already present in each
    # document bundle.  Keep all other audit targets here without duplicating
    # those rows in an organization export.
    if category.key == "audit_events" and "target_type" in table.c:
        statement = statement.where(table.c.target_type != "document")
    if after_id is not None:
        cursor: str | uuid.UUID = after_id
        if session.get_bind().dialect.name == "postgresql":
            cursor = uuid.UUID(after_id)
        statement = statement.where(table.c.id > cursor)
    statement = statement.order_by(table.c.id).limit(limit + 1)

    mappings = list((await session.execute(statement)).mappings().all())
    complete = len(mappings) <= limit
    selected = mappings[:limit]
    records = [
        {str(key): sanitize_export_value(value, key=str(key)) for key, value in row.items()}
        for row in selected
    ]
    last_id = str(selected[-1]["id"]) if selected else after_id
    return OrganizationExportPage(category, records, last_id, complete)


__all__ = [
    "ORGANIZATION_EXPORT_CATEGORIES",
    "REDACTED",
    "OrganizationExportCategory",
    "OrganizationExportPage",
    "collect_organization_export_page",
    "sanitize_export_value",
]
