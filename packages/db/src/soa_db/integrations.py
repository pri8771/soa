"""Integration and mapping-profile models (EXP-001).

An Integration is one outbound destination ("Northstar ERP webhook").
Its configuration splits three ways, deliberately:

- the integration ROOT: type, state, and a credential REFERENCE — never
  a secret value;
- the credential itself in a SEPARATE table, written through one helper
  and read through one helper, absent from every API serialization and
  redacted from repr — configuration payloads and credentials never
  travel together;
- versioned MAPPING PROFILES (draft -> published -> superseded, the
  CFG version discipline): the mapping definition plus the TARGET
  schema the mapped payload must satisfy. Published versions are
  immutable — the flush guard refuses edits — so a delivery can always
  name the exact mapping that produced it.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow
from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)

#: Fail-closed registry: an integration type that is not implemented
#: cannot be configured. Webhook delivery is the EXP epic's target;
#: further connector types join this set when they actually exist.
INTEGRATION_TYPES = frozenset({"webhook"})


class UnknownIntegrationTypeError(Exception):
    def __init__(self, integration_type: str) -> None:
        super().__init__(
            f"unknown integration type {integration_type!r} — supported: "
            f"{', '.join(sorted(INTEGRATION_TYPES))}"
        )


class IntegrationStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Integration(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "integrations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IntegrationStatus.ACTIVE
    )
    #: Delivery endpoint (webhook URL). Not a secret, but operator data.
    endpoint_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    #: REFERENCE to the live credential row — never the secret itself.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    active_mapping_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class IntegrationCredential(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """The credential itself, SEPARATED from configuration. The secret
    column is written by ``store_integration_credential`` and read by
    ``credential_secret_for_delivery`` only; it never appears in audit
    summaries, API responses, or repr."""

    __tablename__ = "integration_credentials"

    integration_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. webhook_hmac_secret
    secret: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - safety net, not behavior
        return (
            f"<IntegrationCredential id={self.id} integration_id={self.integration_id} "
            f"kind={self.kind} secret=[redacted]>"
        )


class MappingProfileVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "mapping_profile_versions"

    integration_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    #: The declarative mapping rules (EXP-002 executes them; no code).
    definition: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    #: JSON Schema the MAPPED payload must satisfy before delivery.
    target_schema: Mapped[dict[str, Any]] = mapped_column(
        PORTABLE_JSON, nullable=False, default=dict
    )
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("integration_id", "version_number"),
        Index(
            "uq_mapping_profile_versions_single_published",
            "integration_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


class IntegrationRepository(ScopedRepository[Integration]):
    model = Integration

    async def get_by_slug(self, slug: str) -> Integration | None:
        stmt = self._scoped_select().where(Integration.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class IntegrationCredentialRepository(ScopedRepository[IntegrationCredential]):
    model = IntegrationCredential


class MappingProfileVersionRepository(ScopedRepository[MappingProfileVersion]):
    model = MappingProfileVersion

    async def list_for_integration(self, integration_id: uuid.UUID) -> list[MappingProfileVersion]:
        stmt = (
            self._scoped_select()
            .where(MappingProfileVersion.integration_id == integration_id)
            .order_by(MappingProfileVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, integration_id: uuid.UUID) -> MappingProfileVersion | None:
        stmt = self._scoped_select().where(
            MappingProfileVersion.integration_id == integration_id,
            MappingProfileVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def create_integration(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    name: str,
    slug: str,
    integration_type: str,
    endpoint_url: str | None = None,
    actor_id: str,
) -> Integration:
    if integration_type not in INTEGRATION_TYPES:
        raise UnknownIntegrationTypeError(integration_type)
    integration = IntegrationRepository(session, context).add(
        Integration(
            name=name, slug=slug, integration_type=integration_type, endpoint_url=endpoint_url
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="integration.created",
        target_type="integration",
        target_id=str(integration.id),
        organization_id=context.organization_id,
        summary={"slug": slug, "type": integration_type},
    )
    return integration


async def store_integration_credential(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    integration: Integration,
    kind: str,
    secret: str,
    actor_id: str,
) -> IntegrationCredential:
    """Store (or rotate) the integration's credential. Any previous
    credential is revoked, the integration keeps only the REFERENCE, and
    the audit event records THAT a credential changed — never its value."""
    if not secret.strip():
        raise ValueError("a credential needs a non-empty secret")
    repo = IntegrationCredentialRepository(session, context)
    now = utcnow()
    previous_id: uuid.UUID | None = None
    if integration.credential_id is not None:
        previous = await repo.get(integration.credential_id)
        if previous is not None and previous.revoked_at is None:
            previous.revoked_at = now
            previous_id = previous.id
    credential = repo.add(
        IntegrationCredential(
            integration_id=integration.id, kind=kind, secret=secret, created_by=actor_id
        )
    )
    await session.flush()
    integration.credential_id = credential.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="integration.credential_rotated" if previous_id else "integration.credential_set",
        target_type="integration",
        target_id=str(integration.id),
        organization_id=context.organization_id,
        # THAT it changed, never WHAT it is.
        summary={"kind": kind, "revoked_credential_id": str(previous_id) if previous_id else None},
    )
    return credential


async def credential_secret_for_delivery(
    session: AsyncSession, context: OrganizationContext, *, integration: Integration
) -> str | None:
    """The ONE sanctioned read path for the secret (delivery signing)."""
    if integration.credential_id is None:
        return None
    credential = await IntegrationCredentialRepository(session, context).get(
        integration.credential_id
    )
    if credential is None or credential.revoked_at is not None:
        return None
    return credential.secret


async def create_mapping_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    integration: Integration,
    definition: dict[str, Any] | None = None,
    target_schema: dict[str, Any] | None = None,
    change_summary: str | None = None,
    actor_id: str,
) -> MappingProfileVersion:
    versions = await MappingProfileVersionRepository(session, context).list_for_integration(
        integration.id
    )
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = MappingProfileVersionRepository(session, context).add(
        MappingProfileVersion(
            integration_id=integration.id,
            version_number=next_number,
            definition=dict(definition or {}),
            target_schema=dict(target_schema or {}),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="integration.mapping_draft_created",
        target_type="mapping_profile_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={"integration_id": str(integration.id), "version_number": next_number},
    )
    return draft


async def publish_mapping_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    integration: Integration,
    draft: MappingProfileVersion,
    actor_id: str,
    now: datetime | None = None,
) -> MappingProfileVersion:
    """Publish a mapping draft: the previous published version is
    superseded (kept, immutable), the integration's active pointer moves,
    and the published definition + target schema freeze forever."""
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    if draft.integration_id != integration.id:
        raise InvalidVersionStateError("draft belongs to a different integration")
    current = now or utcnow()
    previous = await MappingProfileVersionRepository(session, context).get_published(integration.id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        await session.flush()
    draft.state = VersionState.PUBLISHED
    draft.published_at = current
    draft.published_by = actor_id
    integration.active_mapping_version_id = draft.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="integration.mapping_published",
        target_type="mapping_profile_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "integration_id": str(integration.id),
            "version_number": draft.version_number,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft
