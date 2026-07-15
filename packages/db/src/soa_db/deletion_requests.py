"""Durable approval and legal-hold lifecycle for document erasure.

The low-level eraser in :mod:`soa_db.data_deletion` deliberately cannot
decide whether a deletion is authorized.  This module persists that decision:
one bounded request per document, a two-person approval gate, an absolute
legal-hold veto, and a transactional durable job intent.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.advisory import transaction_advisory_lock
from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.documents import DocumentRepository, DocumentState
from soa_db.jobs import enqueue_job
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow

DOCUMENT_DELETION_JOB_TYPE = "document.delete"

DELETION_SETTLED_STATES = frozenset(
    {
        DocumentState.COMPLETED.value,
        DocumentState.REJECTED.value,
        DocumentState.CANCELLED.value,
        DocumentState.FAILED_TERMINAL.value,
        DocumentState.QUARANTINED.value,
        DocumentState.ARCHIVED.value,
    }
)


class DeletionRequestState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class LegalHoldState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"


class DeletionLifecycleError(ValueError):
    """A requested lifecycle action is unsafe or invalid."""


class DeletionRequest(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "deletion_requests"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DeletionRequestState.PENDING_APPROVAL.value
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(200), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approval_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    safe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        # A document has one attributable request/tombstone lifecycle. Repeated
        # HTTP requests return it instead of creating competing approvals.
        Index(
            "uq_deletion_requests_org_document",
            "organization_id",
            "document_id",
            unique=True,
        ),
        CheckConstraint(
            "state IN ('pending_approval','approved','running','failed','completed','cancelled')",
            name="deletion_requests_state_valid",
        ),
    )


class LegalHold(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "legal_holds"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=LegalHoldState.ACTIVE)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    placed_by: Mapped[str] = mapped_column(String(200), nullable=False)
    placed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    released_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        Index(
            "uq_legal_holds_one_active_per_document",
            "organization_id",
            "document_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        CheckConstraint(
            "state IN ('active','released')",
            name="legal_holds_state_valid",
        ),
    )


class DeletionRequestRepository(ScopedRepository[DeletionRequest]):
    model = DeletionRequest

    async def get_for_document(
        self, document_id: uuid.UUID, *, for_update: bool = False
    ) -> DeletionRequest | None:
        statement = self._scoped_select().where(DeletionRequest.document_id == document_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_recent(self, *, limit: int = 100) -> list[DeletionRequest]:
        statement = self._scoped_select().order_by(DeletionRequest.created_at.desc()).limit(limit)
        return list((await self._session.execute(statement)).scalars().all())


class LegalHoldRepository(ScopedRepository[LegalHold]):
    model = LegalHold

    async def active_for_document(
        self, document_id: uuid.UUID, *, for_update: bool = False
    ) -> LegalHold | None:
        statement = self._scoped_select().where(
            LegalHold.document_id == document_id,
            LegalHold.state == LegalHoldState.ACTIVE.value,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_for_document(self, document_id: uuid.UUID) -> list[LegalHold]:
        statement = (
            self._scoped_select()
            .where(LegalHold.document_id == document_id)
            .order_by(LegalHold.placed_at.desc())
        )
        return list((await self._session.execute(statement)).scalars().all())


async def request_document_deletion(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    reason: str,
    actor_id: str,
) -> DeletionRequest:
    normalized = reason.strip()
    if len(normalized) < 3 or len(normalized) > 500:
        raise DeletionLifecycleError("deletion reason must be 3-500 characters")
    await transaction_advisory_lock(
        session, "document-deletion-lifecycle", context.organization_id, document_id
    )
    document = await DocumentRepository(session, context).get(document_id, for_update=True)
    if document is None:
        raise DeletionLifecycleError("document does not exist")
    if document.state not in DELETION_SETTLED_STATES:
        raise DeletionLifecycleError(
            "document deletion requires a settled terminal state; "
            f"the document is {document.state!r}"
        )
    repo = DeletionRequestRepository(session, context)
    existing = await repo.get_for_document(document_id, for_update=True)
    if existing is not None:
        if existing.state != DeletionRequestState.CANCELLED.value:
            return existing
        existing.state = DeletionRequestState.PENDING_APPROVAL.value
        existing.reason = normalized
        existing.requested_by = actor_id
        existing.requested_at = utcnow()
        existing.approved_by = None
        existing.approval_reason = None
        existing.approved_at = None
        existing.completed_at = None
        existing.cancelled_by = None
        existing.cancellation_reason = None
        existing.cancelled_at = None
        existing.safe_error = None
        request = existing
    else:
        request = repo.add(
            DeletionRequest(
                document_id=document_id,
                reason=normalized,
                requested_by=actor_id,
            )
        )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="document.deletion_requested",
        target_type="document",
        target_id=str(document_id),
        organization_id=context.organization_id,
        # The reason is retained in the access-controlled request; audit keeps
        # only the workflow identity so accidental personal data is not copied.
        summary={"deletion_request_id": str(request.id)},
    )
    return request


async def cancel_document_deletion(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    request: DeletionRequest,
    reason: str,
    actor_id: str,
) -> DeletionRequest:
    normalized = reason.strip()
    if len(normalized) < 3 or len(normalized) > 500:
        raise DeletionLifecycleError("cancellation reason must be 3-500 characters")
    await transaction_advisory_lock(
        session,
        "document-deletion-lifecycle",
        context.organization_id,
        request.document_id,
    )
    fresh = await DeletionRequestRepository(session, context).get(request.id, for_update=True)
    if fresh is None:
        raise DeletionLifecycleError("deletion request does not exist")
    request = fresh
    if request.state == DeletionRequestState.CANCELLED.value:
        return request
    if request.state not in (
        DeletionRequestState.PENDING_APPROVAL.value,
        DeletionRequestState.APPROVED.value,
        DeletionRequestState.FAILED.value,
    ):
        raise DeletionLifecycleError("running or completed deletion requests cannot be cancelled")
    request.state = DeletionRequestState.CANCELLED.value
    request.cancelled_by = actor_id
    request.cancellation_reason = normalized
    request.cancelled_at = utcnow()
    request.safe_error = None
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="document.deletion_cancelled",
        target_type="document",
        target_id=str(request.document_id),
        organization_id=context.organization_id,
        summary={"deletion_request_id": str(request.id)},
    )
    return request


async def approve_document_deletion(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    request: DeletionRequest,
    approval_reason: str,
    actor_id: str,
) -> DeletionRequest:
    normalized = approval_reason.strip()
    if len(normalized) < 3 or len(normalized) > 500:
        raise DeletionLifecycleError("approval reason must be 3-500 characters")
    await transaction_advisory_lock(
        session,
        "document-deletion-lifecycle",
        context.organization_id,
        request.document_id,
    )
    fresh = await DeletionRequestRepository(session, context).get(request.id, for_update=True)
    if fresh is None:
        raise DeletionLifecycleError("deletion request does not exist")
    request = fresh
    if request.state != DeletionRequestState.PENDING_APPROVAL.value:
        raise DeletionLifecycleError("only pending deletion requests can be approved")
    if request.requested_by == actor_id:
        raise DeletionLifecycleError("the requester cannot approve their own deletion request")
    if await LegalHoldRepository(session, context).active_for_document(
        request.document_id, for_update=True
    ):
        raise DeletionLifecycleError("an active legal hold blocks document deletion")
    request.state = DeletionRequestState.APPROVED.value
    request.approved_by = actor_id
    request.approval_reason = normalized
    request.approved_at = utcnow()
    request.safe_error = None
    await session.flush()
    await enqueue_job(
        session,
        job_type=DOCUMENT_DELETION_JOB_TYPE,
        organization_id=context.organization_id,
        payload={
            "organization_id": str(context.organization_id),
            "deletion_request_id": str(request.id),
        },
        dedupe_key=f"document-deletion:{request.id}:{request.approved_at.isoformat()}",
        max_attempts=10,
        priority=10,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="document.deletion_approved",
        target_type="document",
        target_id=str(request.document_id),
        organization_id=context.organization_id,
        summary={"deletion_request_id": str(request.id)},
    )
    return request


async def place_legal_hold(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    reason: str,
    actor_id: str,
) -> LegalHold:
    normalized = reason.strip()
    if len(normalized) < 3 or len(normalized) > 500:
        raise DeletionLifecycleError("legal-hold reason must be 3-500 characters")
    await transaction_advisory_lock(
        session, "document-deletion-lifecycle", context.organization_id, document_id
    )
    if await DocumentRepository(session, context).get(document_id, for_update=True) is None:
        raise DeletionLifecycleError("document does not exist")
    repo = LegalHoldRepository(session, context)
    existing = await repo.active_for_document(document_id, for_update=True)
    if existing is not None:
        return existing
    hold = repo.add(LegalHold(document_id=document_id, reason=normalized, placed_by=actor_id))
    await session.flush()
    # A hold placed before execution revokes an approval. Re-approval after
    # release is explicit; the old queue delivery then becomes a safe no-op.
    request = await DeletionRequestRepository(session, context).get_for_document(
        document_id, for_update=True
    )
    if request is not None and request.state in (
        DeletionRequestState.PENDING_APPROVAL.value,
        DeletionRequestState.APPROVED.value,
        DeletionRequestState.FAILED.value,
    ):
        request.state = DeletionRequestState.PENDING_APPROVAL.value
        request.approved_by = None
        request.approval_reason = None
        request.approved_at = None
        request.safe_error = "approval cleared because a legal hold was placed"
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="document.legal_hold_placed",
        target_type="document",
        target_id=str(document_id),
        organization_id=context.organization_id,
        summary={"legal_hold_id": str(hold.id)},
    )
    return hold


async def release_legal_hold(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    hold: LegalHold,
    reason: str,
    actor_id: str,
) -> LegalHold:
    normalized = reason.strip()
    if len(normalized) < 3 or len(normalized) > 500:
        raise DeletionLifecycleError("release reason must be 3-500 characters")
    await transaction_advisory_lock(
        session,
        "document-deletion-lifecycle",
        context.organization_id,
        hold.document_id,
    )
    fresh = await LegalHoldRepository(session, context).get(hold.id, for_update=True)
    if fresh is None:
        raise DeletionLifecycleError("legal hold does not exist")
    hold = fresh
    if hold.state == LegalHoldState.RELEASED.value:
        return hold
    hold.state = LegalHoldState.RELEASED.value
    hold.released_by = actor_id
    hold.release_reason = normalized
    hold.released_at = utcnow()
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="document.legal_hold_released",
        target_type="document",
        target_id=str(hold.document_id),
        organization_id=context.organization_id,
        summary={"legal_hold_id": str(hold.id)},
    )
    return hold


__all__ = [
    "DOCUMENT_DELETION_JOB_TYPE",
    "DeletionLifecycleError",
    "DeletionRequest",
    "DeletionRequestRepository",
    "DeletionRequestState",
    "LegalHold",
    "LegalHoldRepository",
    "LegalHoldState",
    "approve_document_deletion",
    "cancel_document_deletion",
    "place_legal_hold",
    "release_legal_hold",
    "request_document_deletion",
]
