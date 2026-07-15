"""Integration and mapping-profile models (EXP-001).

An Integration is one outbound destination ("Northstar ERP webhook").
Its configuration splits three ways, deliberately:

- the integration ROOT: type, state, and a credential REFERENCE — never
  a secret value;
- the credential row in a SEPARATE table holding a SECRET-STORE
  REFERENCE (SEC-005) — the value itself lives behind the
  ``soa_config.SecretStore`` interface and never touches the tenant
  database; the reference is written through one helper and resolved
  through one helper, absent from every API serialization;
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

from soa_config import SecretNotFoundError, SecretReference, SecretStore
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.external_cleanup import (
    ExternalResourceType,
    register_external_resource_rollback,
)
from soa_db.jobs import enqueue_job
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow, uuid7
from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)

#: Fail-closed registry: an integration type that is not implemented
#: cannot be configured. Webhook delivery is the EXP epic's target;
#: further connector types join this set when they actually exist.
#: ``quickbooks_online`` is the first real ERP connector (EXP-011); the
#: bearer-REST family (NetSuite, Dynamics 365, SAP) follows the same
#: EXP-010 contract.
INTEGRATION_TYPES = frozenset(
    {
        "webhook",
        "quickbooks_online",
        "netsuite",
        "microsoft_dynamics365",
        "sap_s4hana",
    }
)

SECRET_REVOKE_JOB_TYPE = "secret.revoke"


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


_ALLOWED_STATUS_TRANSITIONS = {
    IntegrationStatus.PAUSED: {IntegrationStatus.ACTIVE, IntegrationStatus.ARCHIVED},
    IntegrationStatus.ACTIVE: {IntegrationStatus.PAUSED, IntegrationStatus.ARCHIVED},
    IntegrationStatus.ARCHIVED: set(),
}


class InvalidIntegrationTransitionError(Exception):
    def __init__(self, *, current: str, requested: str) -> None:
        super().__init__(f"integration cannot move from {current!r} to {requested!r}")


class Integration(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "integrations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IntegrationStatus.PAUSED
    )
    #: Delivery endpoint (webhook URL). Not a secret, but operator data.
    endpoint_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    #: REFERENCE to the live credential row — never the secret itself.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    active_mapping_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class IntegrationCredential(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """Credential metadata plus a SECRET-STORE REFERENCE (SEC-005) —
    never the value. ``secret_reference`` is written by
    ``store_integration_credential`` and resolved by
    ``credential_secret_for_delivery`` only; neither the reference nor
    the value appears in audit summaries or API responses."""

    __tablename__ = "integration_credentials"

    integration_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. webhook_hmac_secret
    #: ``secretref://<provider>/<name>`` — resolvable only through the
    #: configured secret store.
    secret_reference: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


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
            name=name,
            slug=slug,
            integration_type=integration_type,
            endpoint_url=endpoint_url,
            status=IntegrationStatus.PAUSED,
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


async def transition_integration(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    integration: Integration,
    requested: IntegrationStatus,
    actor_id: str,
) -> Integration:
    current = IntegrationStatus(integration.status)
    if requested == current:
        return integration
    if requested not in _ALLOWED_STATUS_TRANSITIONS[current]:
        raise InvalidIntegrationTransitionError(current=current, requested=requested)
    integration.status = requested
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action=f"integration.{requested.value}",
        target_type="integration",
        target_id=str(integration.id),
        organization_id=context.organization_id,
        summary={"from": current.value, "to": requested.value},
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
    secret_store: SecretStore,
) -> IntegrationCredential:
    """Store (or rotate) the integration's credential. The VALUE goes
    into the secret store under a fresh name; the database keeps only
    the reference. Any previous credential row is revoked and its
    stored value queued for revocation with it, and the audit event records THAT a
    credential changed — never its value or reference.

    The new value is stored first. The database pointer, old-row revocation,
    and durable ``secret.revoke`` intent then commit atomically. The old
    external value is never deleted before that commit, so a database
    failure cannot leave the live DB pointer referencing a deleted secret.
    The new external value has an idempotent rollback compensation, so a failed
    flush or commit does not leave an orphaned credential."""
    if not secret.strip():
        raise ValueError("a credential needs a non-empty secret")
    repo = IntegrationCredentialRepository(session, context)
    now = utcnow()
    reference = await secret_store.put(
        f"orgs/{context.organization_id}/integrations/{integration.id}/credentials/{uuid7()}",
        secret,
    )

    async def revoke_new_reference() -> None:
        await secret_store.revoke(reference)

    register_external_resource_rollback(
        session,
        organization_id=context.organization_id,
        resource_type=ExternalResourceType.SECRET,
        resource_locator=str(reference),
        cleanup=revoke_new_reference,
    )
    previous_id: uuid.UUID | None = None
    previous_reference: str | None = None
    if integration.credential_id is not None:
        previous = await repo.get(integration.credential_id)
        if previous is not None and previous.revoked_at is None:
            previous.revoked_at = now
            previous_id = previous.id
            previous_reference = previous.secret_reference
    credential = repo.add(
        IntegrationCredential(
            integration_id=integration.id,
            kind=kind,
            secret_reference=str(reference),
            created_by=actor_id,
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
    if previous_id is not None and previous_reference is not None:
        await enqueue_job(
            session,
            job_type=SECRET_REVOKE_JOB_TYPE,
            organization_id=context.organization_id,
            payload={
                "credential_type": "integration",
                "credential_id": str(previous_id),
                "secret_reference": previous_reference,
            },
            dedupe_key=f"secret.revoke:{previous_id}",
            max_attempts=10,
        )
    return credential


async def credential_secret_for_delivery(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    integration: Integration,
    secret_store: SecretStore,
) -> str | None:
    """The ONE sanctioned read path for the secret value (delivery
    signing). ``None`` means no live credential is CONFIGURED; a
    configured credential whose stored value is unexpectedly gone
    raises — that is an inconsistency to surface, not an absence."""
    if integration.credential_id is None:
        return None
    credential = await IntegrationCredentialRepository(session, context).get(
        integration.credential_id
    )
    if credential is None or credential.revoked_at is not None:
        return None
    try:
        return await secret_store.resolve(SecretReference.parse(credential.secret_reference))
    except SecretNotFoundError:
        raise SecretNotFoundError(
            f"integration {integration.id} references credential {credential.id} "
            "but the secret store has no live value behind it — "
            "the credential must be re-set"
        ) from None


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
