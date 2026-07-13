"""Export jobs and delivery attempts (EXP-005).

An ExportJob is the durable intent to deliver ONE approved canonical
payload through ONE integration under ONE pinned mapping version. Three
invariants carry the epic's correctness:

- THE PAYLOAD IS FIXED ACROSS RETRIES. The job references the immutable
  canonical_payloads row (CAN-003) — retries re-deliver, they never
  re-derive, so what the receiver eventually gets is exactly what was
  approved.
- THE BUSINESS KEY IS THE IDEMPOTENCY BOUNDARY. One key per
  (integration, document, run), unique per tenant; creating the same
  export twice returns the existing job, and every retry travels under
  the SAME key so receivers can deduplicate.
- ATTEMPTS ARE APPEND-ONLY EVIDENCE. Each delivery attempt is a new
  numbered row (a flush guard refuses edits); the job's state summarizes
  the latest outcome without erasing history.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String, UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class ExportJobState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


_ALLOWED: dict[str, frozenset[str]] = {
    ExportJobState.PENDING.value: frozenset(
        {ExportJobState.IN_PROGRESS.value, ExportJobState.CANCELLED.value}
    ),
    ExportJobState.IN_PROGRESS.value: frozenset(
        {
            ExportJobState.SUCCEEDED.value,
            ExportJobState.FAILED_RETRYABLE.value,
            ExportJobState.FAILED_TERMINAL.value,
            ExportJobState.CANCELLED.value,
        }
    ),
    # A retryable failure re-enters the queue; retries that keep failing
    # go terminal; operators may cancel.
    ExportJobState.FAILED_RETRYABLE.value: frozenset(
        {
            ExportJobState.PENDING.value,
            ExportJobState.IN_PROGRESS.value,
            ExportJobState.FAILED_TERMINAL.value,
            ExportJobState.CANCELLED.value,
        }
    ),
    ExportJobState.SUCCEEDED.value: frozenset(),
    # Terminal for the machine itself; an operator REPLAY (audited, with
    # a required reason — replay_export_job) may re-queue the same job:
    # same payload, same mapping, same business key.
    ExportJobState.FAILED_TERMINAL.value: frozenset({ExportJobState.PENDING.value}),
    ExportJobState.CANCELLED.value: frozenset({ExportJobState.PENDING.value}),
}


class InvalidExportTransitionError(Exception):
    def __init__(self, job_id: object, source: str, target: str) -> None:
        super().__init__(f"export job {job_id}: transition {source!r} -> {target!r} is not allowed")


class DeliveryAttemptImmutableError(Exception):
    def __init__(self, attempt_id: object) -> None:
        super().__init__(
            f"delivery attempt {attempt_id} is append-only evidence — attempts never change"
        )


def export_business_key(
    integration_id: uuid.UUID, document_id: uuid.UUID, run_id: uuid.UUID
) -> str:
    """The canonical business idempotency key: one export intent per
    (integration, document, run). Retries REUSE it."""
    return f"export:{integration_id}:{document_id}:{run_id}"


class ExportJob(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "export_jobs"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: The APPROVED payload (immutable row) — never re-derived on retry.
    canonical_payload_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: Pinned at creation: the exact frozen mapping this export uses.
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    business_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExportJobState.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "business_key"),
        Index("ix_export_jobs_state", "state"),
    )


class DeliveryAttempt(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "delivery_attempts"

    export_job_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: delivered | retryable_error | terminal_error
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    #: Operator-safe error text; raw responses are stored REDACTED by the
    #: adapter (EXP-007), never here.
    safe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Hash of the request body actually sent, for evidence without content.
    request_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (UniqueConstraint("export_job_id", "attempt_number"),)


@event.listens_for(Session, "before_flush")
def _refuse_attempt_mutation(session: Session, _ctx: object, _instances: object) -> None:
    for obj in session.dirty:
        if isinstance(obj, DeliveryAttempt) and session.is_modified(obj):
            raise DeliveryAttemptImmutableError(obj.id)
    for obj in session.deleted:
        if isinstance(obj, DeliveryAttempt):
            raise DeliveryAttemptImmutableError(obj.id)


class ExportJobRepository(ScopedRepository[ExportJob]):
    model = ExportJob

    async def get_by_business_key(self, business_key: str) -> ExportJob | None:
        stmt = self._scoped_select().where(ExportJob.business_key == business_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_document(self, document_id: uuid.UUID) -> list[ExportJob]:
        stmt = (
            self._scoped_select()
            .where(ExportJob.document_id == document_id)
            .order_by(ExportJob.created_at, ExportJob.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class DeliveryAttemptRepository(ScopedRepository[DeliveryAttempt]):
    model = DeliveryAttempt

    async def list_for_job(self, export_job_id: uuid.UUID) -> list[DeliveryAttempt]:
        stmt = (
            self._scoped_select()
            .where(DeliveryAttempt.export_job_id == export_job_id)
            .order_by(DeliveryAttempt.attempt_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def create_export_job(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    canonical_payload_id: uuid.UUID,
    integration_id: uuid.UUID,
    mapping_version_id: uuid.UUID,
    actor_id: str,
    business_key: str | None = None,
) -> ExportJob:
    """Create the export intent — idempotently. The same business key
    returns the EXISTING job in whatever state it is; duplicate approval
    events cannot fan out into duplicate deliveries."""
    key = business_key or export_business_key(integration_id, document_id, run_id)
    repo = ExportJobRepository(session, context)
    existing = await repo.get_by_business_key(key)
    if existing is not None:
        return existing
    job = repo.add(
        ExportJob(
            document_id=document_id,
            run_id=run_id,
            canonical_payload_id=canonical_payload_id,
            integration_id=integration_id,
            mapping_version_id=mapping_version_id,
            business_key=key,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="export_job.created",
        target_type="export_job",
        target_id=str(job.id),
        organization_id=context.organization_id,
        summary={
            "document_id": str(document_id),
            "run_id": str(run_id),
            "integration_id": str(integration_id),
            "mapping_version_id": str(mapping_version_id),
            "business_key": key,
        },
    )
    return job


async def transition_export_job(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    job: ExportJob,
    to_state: ExportJobState,
    actor_id: str,
    reason: str | None = None,
) -> ExportJob:
    if to_state.value not in _ALLOWED.get(job.state, frozenset()):
        raise InvalidExportTransitionError(job.id, job.state, to_state.value)
    from_state = job.state
    job.state = to_state.value
    if reason is not None:
        job.last_error = reason[:500]
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="export_job.state_changed",
        target_type="export_job",
        target_id=str(job.id),
        organization_id=context.organization_id,
        summary={"from": from_state, "to": to_state.value, "reason": reason},
    )
    return job


async def replay_export_job(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    job: ExportJob,
    actor_id: str,
    reason: str,
) -> ExportJob:
    """Operator replay of a settled (failed_terminal/cancelled) export:
    the SAME job re-queues — same payload reference, same pinned mapping,
    same business key — with the reason on the audit record. Nothing is
    re-extracted or re-derived."""
    if not reason.strip():
        raise ValueError("replay needs a reason")
    if job.state not in (
        ExportJobState.FAILED_TERMINAL.value,
        ExportJobState.CANCELLED.value,
    ):
        raise InvalidExportTransitionError(job.id, job.state, "replayed")
    from_state = job.state
    job.state = ExportJobState.PENDING.value
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="export_job.replayed",
        target_type="export_job",
        target_id=str(job.id),
        organization_id=context.organization_id,
        summary={"from": from_state, "reason": reason.strip(), "business_key": job.business_key},
    )
    return job


async def record_delivery_attempt(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    job: ExportJob,
    outcome: str,
    response_status: int | None = None,
    safe_error: str | None = None,
    request_sha256: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> DeliveryAttempt:
    """Append the next numbered attempt. The job's counter moves; the
    attempt rows never do."""
    if outcome not in ("delivered", "retryable_error", "terminal_error"):
        raise ValueError("attempt outcomes are 'delivered', 'retryable_error', or 'terminal_error'")
    attempt = DeliveryAttemptRepository(session, context).add(
        DeliveryAttempt(
            export_job_id=job.id,
            attempt_number=job.attempt_count + 1,
            started_at=started_at or utcnow(),
            finished_at=finished_at or utcnow(),
            outcome=outcome,
            response_status=response_status,
            safe_error=safe_error[:500] if safe_error else None,
            request_sha256=request_sha256,
        )
    )
    job.attempt_count += 1
    if safe_error:
        job.last_error = safe_error[:500]
    await session.flush()
    return attempt


__all__ = [
    "DeliveryAttempt",
    "DeliveryAttemptImmutableError",
    "DeliveryAttemptRepository",
    "ExportJob",
    "ExportJobRepository",
    "ExportJobState",
    "InvalidExportTransitionError",
    "create_export_job",
    "export_business_key",
    "record_delivery_attempt",
    "replay_export_job",
    "transition_export_job",
]
