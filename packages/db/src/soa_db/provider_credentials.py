"""Tenant-managed credentials for document-AI providers.

Only immutable secret-store references are persisted here.  Secret values enter
through :func:`store_provider_credential`, are written directly to the configured
``SecretStore``, and never have a database or API read path.  Rotation creates a
new reference and retains the superseded value so already-published policies and
in-flight runs keep their immutable execution contract.  Explicit revocation is
performed by a durable post-commit job.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String, Text, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_config import SecretStore
from soa_db.advisory import transaction_advisory_lock
from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.external_cleanup import (
    ExternalResourceType,
    register_external_resource_rollback,
)
from soa_db.jobs import enqueue_job
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import UTCDateTime, utcnow, uuid7

PROVIDER_SECRET_REVOKE_JOB_TYPE = "secret.revoke"
PROVIDER_CREDENTIAL_TYPE = "provider"


class ProviderCredentialStatus(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ProviderCredentialConflictError(ValueError):
    """A credential mutation would violate immutable policy safety."""


class ProviderCredential(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    Base,
):
    """Credential metadata and an opaque secret-store reference, never a value."""

    __tablename__ = "provider_credentials"

    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    secret_reference: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index(
            "uq_provider_credentials_current",
            "organization_id",
            "provider_name",
            unique=True,
            postgresql_where=text("superseded_at IS NULL AND revoked_at IS NULL"),
            sqlite_where=text("superseded_at IS NULL AND revoked_at IS NULL"),
        ),
        Index("ix_provider_credentials_org_provider", "organization_id", "provider_name"),
    )

    @property
    def status(self) -> ProviderCredentialStatus:
        if self.revoked_at is not None:
            return ProviderCredentialStatus.REVOKED
        if self.superseded_at is not None:
            return ProviderCredentialStatus.SUPERSEDED
        return ProviderCredentialStatus.CURRENT


class ProviderCredentialRepository(ScopedRepository[ProviderCredential]):
    model = ProviderCredential

    async def get_current(
        self, provider_name: str, *, for_update: bool = False
    ) -> ProviderCredential | None:
        statement = self._scoped_select().where(
            ProviderCredential.provider_name == provider_name,
            ProviderCredential.superseded_at.is_(None),
            ProviderCredential.revoked_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_locked(self, credential_id: uuid.UUID) -> ProviderCredential | None:
        statement = self._scoped_select().where(ProviderCredential.id == credential_id)
        return (await self._session.execute(statement.with_for_update())).scalar_one_or_none()

    async def list_for_provider(self, provider_name: str) -> list[ProviderCredential]:
        statement = (
            self._scoped_select()
            .where(ProviderCredential.provider_name == provider_name)
            .order_by(ProviderCredential.created_at.desc(), ProviderCredential.id.desc())
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def list_all(self) -> list[ProviderCredential]:
        statement = self._scoped_select().order_by(
            ProviderCredential.provider_name,
            ProviderCredential.created_at.desc(),
            ProviderCredential.id.desc(),
        )
        return list((await self._session.execute(statement)).scalars().all())


async def store_provider_credential(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    provider_name: str,
    label: str,
    kind: str,
    secret: str,
    actor_id: str,
    secret_store: SecretStore,
) -> tuple[ProviderCredential, uuid.UUID | None]:
    """Create or rotate a provider credential without invalidating old pins.

    A new immutable secret reference is created first.  The prior current row is
    marked superseded in the same database transaction as the new metadata row.
    Its external value is deliberately retained until an administrator explicitly
    revokes it after removing policy references.
    """

    if not provider_name.strip() or provider_name != provider_name.strip():
        raise ValueError("provider_name must be non-empty and trimmed")
    if not label.strip() or label != label.strip():
        raise ValueError("credential label must be non-empty and trimmed")
    if not kind.strip() or kind != kind.strip():
        raise ValueError("credential kind must be non-empty and trimmed")
    if not secret.strip():
        raise ValueError("a credential needs a non-empty secret")

    # Lock the logical current slot, including the initial empty case. A row
    # lock alone cannot prevent two replicas from both creating the first row.
    await transaction_advisory_lock(
        session,
        "provider-current-credential",
        context.organization_id,
        provider_name,
    )
    repository = ProviderCredentialRepository(session, context)
    previous = await repository.get_current(provider_name, for_update=True)
    reference = await secret_store.put(
        f"orgs/{context.organization_id}/providers/{provider_name}/credentials/{uuid7()}",
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
    now = utcnow()
    if previous is not None:
        previous.superseded_at = now
    credential = repository.add(
        ProviderCredential(
            provider_name=provider_name,
            label=label,
            kind=kind,
            secret_reference=str(reference),
            created_by=actor_id,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action=(
            "provider.credential_rotated" if previous is not None else "provider.credential_set"
        ),
        target_type="provider_credential",
        target_id=str(credential.id),
        organization_id=context.organization_id,
        summary={
            "provider_name": provider_name,
            "kind": kind,
            "previous_credential_id": str(previous.id) if previous is not None else None,
            # No secret value or secret-store reference is audit-safe.
        },
    )
    return credential, previous.id if previous is not None else None


async def revoke_provider_credential(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    credential: ProviderCredential,
    reason: str,
    actor_id: str,
    referenced_policy_ids: Sequence[uuid.UUID],
    force: bool = False,
) -> ProviderCredential:
    """Mark a credential revoked and enqueue external deletion after commit.

    Normal revocation refuses credentials named by any immutable or draft policy.
    ``force`` is an explicit break-glass path for a compromised key: affected
    pinned executions fail closed once the durable revocation job runs.
    """

    if not reason.strip():
        raise ValueError("credential revocation requires a reason")
    repository = ProviderCredentialRepository(session, context)
    locked = await repository.get_locked(credential.id)
    if locked is None:
        raise ProviderCredentialConflictError("provider credential no longer exists")
    if locked.revoked_at is not None:
        return locked
    if referenced_policy_ids and not force:
        raise ProviderCredentialConflictError(
            "credential is referenced by policy versions; rotate and remove those references "
            "before revoking, or use the audited force option for a compromised key"
        )

    locked.revoked_at = utcnow()
    locked.revocation_reason = reason.strip()[:500]
    await session.flush()
    await enqueue_job(
        session,
        job_type=PROVIDER_SECRET_REVOKE_JOB_TYPE,
        organization_id=context.organization_id,
        payload={
            "credential_type": PROVIDER_CREDENTIAL_TYPE,
            "credential_id": str(locked.id),
            "secret_reference": locked.secret_reference,
        },
        dedupe_key=f"secret.revoke:provider:{locked.id}",
        max_attempts=10,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="provider.credential_force_revoked" if force else "provider.credential_revoked",
        target_type="provider_credential",
        target_id=str(locked.id),
        organization_id=context.organization_id,
        summary={
            "provider_name": locked.provider_name,
            "force": force,
            "referenced_policy_ids": [str(item) for item in referenced_policy_ids],
            "reason": locked.revocation_reason,
        },
    )
    return locked


async def credential_reference_is_live(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    credential_id: uuid.UUID,
    provider_name: str,
    require_current: bool,
) -> ProviderCredential | None:
    """Lock and verify a server-bound policy credential."""

    statement = (
        select(ProviderCredential)
        .where(
            ProviderCredential.organization_id == context.organization_id,
            ProviderCredential.id == credential_id,
            ProviderCredential.provider_name == provider_name,
            ProviderCredential.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if require_current:
        statement = statement.where(ProviderCredential.superseded_at.is_(None))
    return (await session.execute(statement)).scalar_one_or_none()


__all__ = [
    "PROVIDER_CREDENTIAL_TYPE",
    "PROVIDER_SECRET_REVOKE_JOB_TYPE",
    "ProviderCredential",
    "ProviderCredentialConflictError",
    "ProviderCredentialRepository",
    "ProviderCredentialStatus",
    "credential_reference_is_live",
    "revoke_provider_credential",
    "store_provider_credential",
]
