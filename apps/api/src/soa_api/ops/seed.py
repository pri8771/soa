"""Idempotently load the deterministic Northstar development tenant."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.domain.identity import Membership, MembershipStatus, User
from soa_api.domain.policies import PolicyType, PolicyVersion
from soa_api.domain.processes import Process, ProcessVersion
from soa_api.domain.rbac import SYSTEM_ROLE_TEMPLATES, Role, RoleAssignment
from soa_api.domain.rules import RuleSetVersion
from soa_api.domain.schemas import SchemaVersion
from soa_api.domain.streams import Stream, StreamVersion, resolve_snapshot
from soa_api.domain.tenancy import Organization, Workspace
from soa_api.domain.versioning import VersionState
from soa_api.settings import ApiSettings, load_settings
from soa_db import DatabaseSessions, utcnow
from soa_db.catalogs import Catalog, CatalogRecord, CatalogVersion
from soa_db.documents import Document, SourceChannel
from soa_db.engine import create_database_engine
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_fixtures import DEMO_TENANT, stable_id

ACTOR = "system:seed"

#: Env override for the seeded provider policy's primary provider. Defaults
#: to the fixture's value (the deterministic mock) so tests and CI seed a
#: self-contained tenant. Set it to a registered provider name — e.g.
#: ``local-openai-compatible`` (a local Ollama endpoint) or a hosted BYO-key
#: provider — to seed a demo tenant whose documents extract with a real model.
SEED_EXTRACTION_PROVIDER_ENV = "SOA_SEED_EXTRACTION_PROVIDER"


def _provider_policy_definition(base: dict[str, Any]) -> dict[str, Any]:
    override = os.environ.get(SEED_EXTRACTION_PROVIDER_ENV, "").strip()
    if not override:
        return base
    return {**base, "provider_name": override}


@dataclass(frozen=True)
class DatabaseSeedReport:
    created: dict[str, int]
    existing: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.created.values()) + sum(self.existing.values())


def ensure_seed_allowed(settings: ApiSettings) -> None:
    """Refuse deterministic demo data in staging and production."""
    if not settings.is_development_like:
        raise RuntimeError(
            f"database seeding is development/test-only; refusing {settings.environment.value}"
        )


async def _add_once(
    session: AsyncSession,
    model: type[Any],
    fixture_id: uuid.UUID,
    values: dict[str, Any],
    created: Counter[str],
    existing: Counter[str],
) -> Any:
    kind = model.__tablename__
    current = await session.get(model, fixture_id)
    if current is not None:
        existing[kind] += 1
        return current
    entity = model(id=fixture_id, **values)
    session.add(entity)
    await session.flush()
    created[kind] += 1
    return entity


def _slug(alias: str) -> str:
    return alias.split(":", 1)[1]


def _catalog_shape(kind: str) -> tuple[str, str]:
    return {
        "customers": ("customers", "Customer"),
        "ship_tos": ("custom", "Ship-to"),
        "materials": ("products", "Material"),
        "uoms": ("units", "Unit"),
    }[kind]


def _catalog_record(record: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    source_id = str(record.get("code") or record.get("sku"))
    display_name = str(record.get("name") or record.get("description"))
    attributes = {
        key: value
        for key, value in record.items()
        if key not in {"code", "sku", "name", "description"}
    }
    return source_id, display_name, attributes


async def seed_database(db: DatabaseSessions) -> DatabaseSeedReport:
    """Create every demo record once using stable fixture-derived UUIDs."""
    created: Counter[str] = Counter()
    existing: Counter[str] = Counter()
    tenant = DEMO_TENANT
    now = utcnow()

    async with db.session_scope() as session:
        organization = await _add_once(
            session,
            Organization,
            tenant.organization.id,
            {"name": tenant.organization.data["name"], "slug": "northstar"},
            created,
            existing,
        )
        await bind_tenant(session, organization.id)
        context = OrganizationContext(organization_id=organization.id)

        await _add_once(
            session,
            Workspace,
            tenant.workspace.id,
            {
                "organization_id": organization.id,
                "name": tenant.workspace.data["name"],
                "slug": "europe",
            },
            created,
            existing,
        )

        roles: dict[str, Role] = {}
        for role_slug, permissions in SYSTEM_ROLE_TEMPLATES.items():
            roles[role_slug] = await _add_once(
                session,
                Role,
                stable_id(f"role:{role_slug}"),
                {
                    "organization_id": context.organization_id,
                    "name": role_slug.replace("-", " ").title(),
                    "slug": role_slug,
                    "is_system": True,
                    "permissions": sorted(permissions),
                },
                created,
                existing,
            )

        role_aliases = {"admin": "org-admin", "integration_admin": "integration-admin"}
        for fixture in tenant.users:
            email = str(fixture.data["email"])
            user = await _add_once(
                session,
                User,
                fixture.id,
                {
                    "identity_key": f"soa-dev|{fixture.id}",
                    "email": email,
                    "display_name": fixture.alias,
                },
                created,
                existing,
            )
            membership_id = stable_id(f"membership:{fixture.alias}")
            membership = await _add_once(
                session,
                Membership,
                membership_id,
                {
                    "organization_id": context.organization_id,
                    "user_id": user.id,
                    "invited_email": email,
                    "status": MembershipStatus.ACTIVE.value,
                    "accepted_at": now,
                },
                created,
                existing,
            )
            requested_role = str(fixture.data["role"])
            role_slug = role_aliases.get(requested_role, requested_role)
            await _add_once(
                session,
                RoleAssignment,
                stable_id(f"assignment:{fixture.alias}:{role_slug}"),
                {
                    "organization_id": context.organization_id,
                    "membership_id": membership.id,
                    "role_id": roles[role_slug].id,
                },
                created,
                existing,
            )

        process = await _add_once(
            session,
            Process,
            tenant.process.id,
            {
                "organization_id": context.organization_id,
                "name": tenant.process.data["name"],
                "slug": "sales-orders",
                "active_version_id": stable_id("process-version:sales-orders:1"),
            },
            created,
            existing,
        )
        process_version = await _add_once(
            session,
            ProcessVersion,
            stable_id("process-version:sales-orders:1"),
            {
                "organization_id": context.organization_id,
                "process_id": process.id,
                "version_number": 1,
                "definition": {
                    "schema_version_id": str(tenant.schema.id),
                    "rule_set_version_id": str(tenant.rules.id),
                    "provider_policy_version_id": str(tenant.provider_policy.id),
                },
                "state": VersionState.PUBLISHED.value,
                "change_summary": "Deterministic development seed",
                "published_at": now,
                "published_by": ACTOR,
            },
            created,
            existing,
        )

        streams: list[Stream] = []
        for fixture in tenant.streams:
            stream_version_id = stable_id(f"stream-version:{_slug(fixture.alias)}:1")
            overrides = {"languages": [fixture.data["language"]]}
            stream = await _add_once(
                session,
                Stream,
                fixture.id,
                {
                    "organization_id": context.organization_id,
                    "process_id": process.id,
                    "name": fixture.data["name"],
                    "slug": _slug(fixture.alias),
                    "active_version_id": stream_version_id,
                },
                created,
                existing,
            )
            await _add_once(
                session,
                StreamVersion,
                stream_version_id,
                {
                    "organization_id": context.organization_id,
                    "stream_id": stream.id,
                    "version_number": 1,
                    "overrides": overrides,
                    "resolved_snapshot": resolve_snapshot(process_version, overrides),
                    "pinned_process_version_id": process_version.id,
                    "state": VersionState.PUBLISHED.value,
                    "change_summary": "Deterministic development seed",
                    "published_at": now,
                    "published_by": ACTOR,
                },
                created,
                existing,
            )
            streams.append(stream)

        await _add_once(
            session,
            SchemaVersion,
            tenant.schema.id,
            {
                "organization_id": context.organization_id,
                "process_id": process.id,
                "version_number": 1,
                "definition": tenant.schema.data,
                "state": VersionState.PUBLISHED.value,
                "change_summary": "Deterministic development seed",
                "published_at": now,
                "published_by": ACTOR,
            },
            created,
            existing,
        )
        await _add_once(
            session,
            RuleSetVersion,
            tenant.rules.id,
            {
                "organization_id": context.organization_id,
                "process_id": process.id,
                "version_number": 1,
                "definition": tenant.rules.data,
                "state": VersionState.PUBLISHED.value,
                "change_summary": "Deterministic development seed",
                "published_at": now,
                "published_by": ACTOR,
            },
            created,
            existing,
        )
        await _add_once(
            session,
            PolicyVersion,
            tenant.provider_policy.id,
            {
                "organization_id": context.organization_id,
                "policy_type": PolicyType.PROVIDER.value,
                "version_number": 1,
                "definition": _provider_policy_definition(tenant.provider_policy.data),
                "state": VersionState.PUBLISHED.value,
                "change_summary": "Deterministic development seed",
                "published_at": now,
                "published_by": ACTOR,
            },
            created,
            existing,
        )

        for fixture in tenant.catalogs:
            fixture_kind = str(fixture.data["kind"])
            catalog_type, label = _catalog_shape(fixture_kind)
            catalog = await _add_once(
                session,
                Catalog,
                fixture.id,
                {
                    "organization_id": context.organization_id,
                    "name": f"{label} catalog",
                    "slug": _slug(fixture.alias),
                    "catalog_type": catalog_type,
                    "source": "manual",
                    "active_version_id": stable_id(f"catalog-version:{_slug(fixture.alias)}:1"),
                    "created_by": ACTOR,
                },
                created,
                existing,
            )
            version = await _add_once(
                session,
                CatalogVersion,
                stable_id(f"catalog-version:{_slug(fixture.alias)}:1"),
                {
                    "organization_id": context.organization_id,
                    "catalog_id": catalog.id,
                    "version_number": 1,
                    # Populate the immutable record set while the version is
                    # still a draft. Publishing first would make the seed
                    # itself violate the same history guarantee enforced for
                    # every other catalog writer.
                    "state": VersionState.DRAFT.value,
                    "record_count": len(fixture.data["records"]),
                    "change_summary": "Deterministic development seed",
                    "published_at": None,
                    "published_by": None,
                },
                created,
                existing,
            )
            for record in fixture.data["records"]:
                source_id, display_name, attributes = _catalog_record(record)
                await _add_once(
                    session,
                    CatalogRecord,
                    stable_id(f"catalog-record:{_slug(fixture.alias)}:{source_id}"),
                    {
                        "organization_id": context.organization_id,
                        "catalog_version_id": version.id,
                        "source_id": source_id,
                        "display_name": display_name,
                        "attributes": attributes,
                        "aliases": [],
                    },
                    created,
                    existing,
                )
            if version.state == VersionState.DRAFT.value:
                version.state = VersionState.PUBLISHED.value
                version.published_at = now
                version.published_by = ACTOR
                await session.flush()

        primary_stream = streams[0]
        for fixture in tenant.documents:
            content = fixture.text.encode()
            await _add_once(
                session,
                Document,
                fixture.id,
                {
                    "organization_id": context.organization_id,
                    "stream_id": primary_stream.id,
                    "state": "received",
                    "source_channel": SourceChannel.UPLOAD.value,
                    "original_filename": fixture.data["filename"],
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "content_type": "application/pdf",
                    "client_reference": fixture.alias,
                    "source_metadata": {"uploader": ACTOR},
                },
                created,
                existing,
            )

    return DatabaseSeedReport(created=dict(created), existing=dict(existing))


async def _run() -> int:
    settings = load_settings()
    ensure_seed_allowed(settings)
    db = DatabaseSessions(create_database_engine(settings.database_url))
    try:
        report = await seed_database(db)
    finally:
        await db.dispose()
    print(
        f"Northstar seed complete: {sum(report.created.values())} created, "
        f"{sum(report.existing.values())} already present"
    )
    for kind in sorted(set(report.created) | set(report.existing)):
        print(f"  {kind}: +{report.created.get(kind, 0)} / ={report.existing.get(kind, 0)}")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception as error:
        print(
            f"Seed failed safely ({type(error).__name__}). Run `make migrate` and verify "
            "SOA_API_DATABASE_URL."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
