"""Durable reconciliation for external resources created by rolled-back work.

Object stores and secret managers cannot join the database transaction.  A
caller therefore registers an idempotent cleanup immediately after creating
an external resource.  If that first compensation fails after the SQL
transaction rolls back, this module persists only the opaque object key or
secret reference and atomically enqueues a bounded retry job.  Secret values
never enter this table, a queue payload, an audit event, or a log record.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_config import SecretReference
from soa_db.base import Base
from soa_db.engine import register_rollback_action
from soa_db.jobs import enqueue_job
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.tenant_guard import bind_tenant
from soa_db.types import GUID, UTCDateTime, utcnow

logger = logging.getLogger(__name__)

EXTERNAL_CLEANUP_JOB_TYPE = "external.cleanup"
MAX_CLEANUP_DISPATCHES = 3
CleanupAction = Callable[[], Awaitable[None]]


class ExternalResourceType(StrEnum):
    OBJECT = "object"
    SECRET = "secret"


class ExternalCleanupState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    MANUAL_INTERVENTION = "manual_intervention"


class ExternalCleanupIntent(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    """A bounded, tenant-owned retry intent containing no resource value."""

    __tablename__ = "external_cleanup_intents"

    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Opaque locator only: an object key or SecretReference, never object bytes
    # or a secret value. The column is deliberately absent from every API.
    resource_locator: Mapped[str | None] = mapped_column(Text(), nullable=True)
    locator_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExternalCleanupState.PENDING
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    dispatch_count: Mapped[int] = mapped_column(nullable=False, default=1)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_reconciled_job_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('object','secret')",
            name="external_cleanup_resource_type_valid",
        ),
        CheckConstraint(
            "state IN ('pending','completed','manual_intervention')",
            name="external_cleanup_state_valid",
        ),
        CheckConstraint(
            "state = 'completed' OR resource_locator IS NOT NULL",
            name="external_cleanup_live_locator_present",
        ),
        CheckConstraint("attempts >= 0", name="external_cleanup_attempts_non_negative"),
        CheckConstraint(
            "dispatch_count >= 1 AND dispatch_count <= 3",
            name="external_cleanup_dispatch_count_bounded",
        ),
        UniqueConstraint(
            "organization_id",
            "resource_type",
            "locator_sha256",
            name="uq_external_cleanup_resource",
        ),
        Index("ix_external_cleanup_org_state", "organization_id", "state"),
    )


class ExternalCleanupIntentRepository(ScopedRepository[ExternalCleanupIntent]):
    model = ExternalCleanupIntent

    async def get_by_locator(
        self,
        resource_type: ExternalResourceType,
        locator_sha256: str,
        *,
        for_update: bool = False,
    ) -> ExternalCleanupIntent | None:
        statement = self._scoped_select().where(
            ExternalCleanupIntent.resource_type == resource_type.value,
            ExternalCleanupIntent.locator_sha256 == locator_sha256,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()


def validate_resource_locator(
    organization_id: uuid.UUID,
    resource_type: ExternalResourceType,
    locator: str,
) -> None:
    """Fail closed before persisting or deleting a cross-tenant locator."""

    if not locator or len(locator) > 2_048 or "\x00" in locator:
        raise ValueError("external cleanup locator is empty or exceeds its safe bound")
    tenant_prefix = f"orgs/{organization_id}/"
    if resource_type is ExternalResourceType.OBJECT:
        if not locator.startswith(tenant_prefix):
            raise ValueError("external object cleanup locator is outside the job tenant")
        if any(segment in {"", ".", ".."} for segment in locator.split("/")):
            raise ValueError("external object cleanup locator is not a canonical object key")
        return
    reference = SecretReference.parse(locator)
    if not reference.name.startswith(tenant_prefix):
        raise ValueError("external secret cleanup reference is outside the job tenant")


def _locator_digest(locator: str) -> str:
    return hashlib.sha256(locator.encode("utf-8")).hexdigest()


def safe_cleanup_error(resource_type: ExternalResourceType, error: BaseException) -> str:
    """Return a useful diagnostic without copying provider messages."""

    return f"{resource_type.value} cleanup failed ({type(error).__name__})"[:500]


async def stage_external_cleanup_intent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    resource_type: ExternalResourceType,
    resource_locator: str,
    safe_error: str,
) -> ExternalCleanupIntent:
    """Persist one cleanup intent and its first queue dispatch atomically."""

    validate_resource_locator(organization_id, resource_type, resource_locator)
    await bind_tenant(session, organization_id)
    context = OrganizationContext(organization_id=organization_id)
    repository = ExternalCleanupIntentRepository(session, context)
    digest = _locator_digest(resource_locator)
    existing = await repository.get_by_locator(resource_type, digest, for_update=True)
    if existing is not None:
        return existing

    intent = repository.add(
        ExternalCleanupIntent(
            resource_type=resource_type.value,
            resource_locator=resource_locator,
            locator_sha256=digest,
            state=ExternalCleanupState.PENDING.value,
            attempts=0,
            dispatch_count=1,
            last_error=safe_error[:500],
        )
    )
    await session.flush()
    await enqueue_job(
        session,
        job_type=EXTERNAL_CLEANUP_JOB_TYPE,
        organization_id=organization_id,
        payload={
            "organization_id": str(organization_id),
            "cleanup_intent_id": str(intent.id),
            "dispatch": 1,
        },
        dedupe_key=f"external.cleanup:{intent.id}:1",
        max_attempts=5,
        priority=20,
    )
    return intent


async def complete_external_cleanup_intent_for_locator(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    resource_type: ExternalResourceType,
    resource_locator: str,
) -> bool:
    """Complete an independently satisfied intent and erase its locator."""

    validate_resource_locator(organization_id, resource_type, resource_locator)
    await bind_tenant(session, organization_id)
    repository = ExternalCleanupIntentRepository(
        session, OrganizationContext(organization_id=organization_id)
    )
    intent = await repository.get_by_locator(
        resource_type, _locator_digest(resource_locator), for_update=True
    )
    if intent is None:
        return False
    if intent.state != ExternalCleanupState.COMPLETED.value:
        intent.state = ExternalCleanupState.COMPLETED.value
        intent.completed_at = utcnow()
    intent.resource_locator = None
    intent.last_error = None
    return True


def register_external_resource_rollback(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    resource_type: ExternalResourceType,
    resource_locator: str,
    cleanup: CleanupAction,
) -> None:
    """Register cleanup plus a durable fallback for a failed compensation.

    The fallback runs only after the owning SQL transaction has rolled back.
    It starts a fresh transaction on the still-open request/worker session and
    explicitly commits the intent and queue row before the original exception
    is re-raised by ``DatabaseSessions.session_scope``.
    """

    validate_resource_locator(organization_id, resource_type, resource_locator)

    async def compensate_or_persist() -> None:
        try:
            await cleanup()
        except Exception as error:
            diagnostic = safe_cleanup_error(resource_type, error)
            try:
                await stage_external_cleanup_intent(
                    session,
                    organization_id=organization_id,
                    resource_type=resource_type,
                    resource_locator=resource_locator,
                    safe_error=diagnostic,
                )
                await session.commit()
            except Exception as persistence_error:
                # Do not replace the original application/database exception.
                # This is the only irreducible gap: if both the external system
                # and database are unavailable, no process can durably record
                # work. The constant log message intentionally omits locators.
                try:
                    await session.rollback()
                except Exception:
                    pass
                logger.error(
                    "could not persist failed external cleanup compensation",
                    extra={
                        "resource_type": resource_type.value,
                        "error_type": type(persistence_error).__name__,
                    },
                )
            else:
                logger.warning(
                    "persisted external cleanup compensation for retry",
                    extra={"resource_type": resource_type.value},
                )

    register_rollback_action(session, compensate_or_persist)


__all__ = [
    "EXTERNAL_CLEANUP_JOB_TYPE",
    "MAX_CLEANUP_DISPATCHES",
    "ExternalCleanupIntent",
    "ExternalCleanupIntentRepository",
    "ExternalCleanupState",
    "ExternalResourceType",
    "complete_external_cleanup_intent_for_locator",
    "register_external_resource_rollback",
    "safe_cleanup_error",
    "stage_external_cleanup_intent",
    "validate_resource_locator",
]
