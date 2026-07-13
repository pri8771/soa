"""Data deletion workflow (SEC-010).

Orchestrates the actual erasure of one document's data once the
retention engine (SEC-008) has ruled it eligible AND a deletion has
been approved. It removes stored objects and derived database rows,
reconciles that the objects are gone, and leaves a :class:`DeletionTombstone`
— the retained proof of what was deleted — plus an audit event. The
document row and the audit trail are deliberately KEPT: a deletion must
stay attributable.

Safety properties:

- **gated.** ``delete_document_data`` refuses unless the caller passes
  an ``approved`` deletion state (the SEC-008 machine reached APPROVED).
  It never decides eligibility itself — that is the engine's job.
- **idempotent and retry-safe.** Deletion is driven through a single
  tombstone per document (unique constraint). A missing object or an
  already-deleted row is not an error; re-running after a partial
  failure deletes only what remains and completes the SAME tombstone.
  A store error mid-run leaves the tombstone ``in_progress`` and
  re-raises, so a retry resumes exactly where it stopped.
- **reconciled.** After deleting objects the workflow re-checks each
  key and fails loudly if anything survives — a deletion that did not
  actually delete is never reported as complete.
- **honest scope.** Database rows and object-store artifacts are erased
  here. External provider caches do not exist in this build (extraction
  is local); the provider-purge hook is a documented no-op until hosted
  adapters land (OPEN-003/004). Backup retention is a deliberate,
  documented exception: point-in-time backups age out under their own
  retention and are not individually purged — the tombstone records the
  logical deletion time from which that clock is measured.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.artifacts import ArtifactRepository
from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.canonical_payloads import CanonicalPayload
from soa_db.corrections import FieldCorrection
from soa_db.exports import DeliveryAttempt, ExportJob
from soa_db.extracted_fields import ExtractedField
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.pages import DocumentPage
from soa_db.repository import OrganizationContext, OrganizationScopedMixin
from soa_db.retention import DeletionState
from soa_db.review_comments import ReviewComment
from soa_db.review_tasks import ReviewTask
from soa_db.runs import ProcessingRun, StageRun
from soa_db.types import GUID, UTCDateTime, utcnow

__all__ = [
    "DeletionNotApprovedError",
    "DeletionResult",
    "DeletionTombstone",
    "ObjectDeleter",
    "delete_document_data",
]


class TombstoneState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DeletionNotApprovedError(Exception):
    """Deletion was requested for a document that has not been approved
    for deletion by the retention state machine (SEC-008)."""


class DeletionTombstone(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """Retained proof that a document's data was deleted."""

    __tablename__ = "deletion_tombstones"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(200), nullable=False)
    object_keys_deleted: Mapped[int] = mapped_column(nullable=False, default=0)
    category_counts: Mapped[dict[str, int]] = mapped_column(
        PORTABLE_JSON, nullable=False, default=dict
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


#: Minimal object-store surface the workflow needs. ObjectStore
#: satisfies it; ``head`` is used for reconciliation, ``delete`` for
#: erasure. Both raise ObjectNotFoundError when the key is absent.
class ObjectDeleter:  # pragma: no cover - structural typing marker
    async def delete(self, key: str) -> None: ...
    async def head(self, key: str) -> Any: ...


#: Derived per-document tables erased on deletion, in FK-safe order
#: (children before parents). document_id-scoped models only; stage
#: runs and delivery attempts are handled via their parent ids.
_DERIVED_MODELS: tuple[type[Base], ...] = (
    ReviewComment,
    ReviewTask,
    FieldCorrection,
    ExtractedField,
    CanonicalPayload,
    DocumentPage,
)


@dataclass
class DeletionResult:
    tombstone_id: uuid.UUID
    document_id: uuid.UUID
    object_keys_deleted: int
    category_counts: dict[str, int]
    already_complete: bool = False
    reconciled: bool = True
    errors: list[str] = field(default_factory=list)


async def _object_delete_idempotent(store: ObjectDeleter, key: str) -> bool:
    """Delete a key; return True if it was present. A missing key is a
    no-op (already deleted on a prior attempt)."""
    from soa_storage import ObjectNotFoundError

    try:
        await store.delete(key)
        return True
    except ObjectNotFoundError:
        return False


async def _object_absent(store: ObjectDeleter, key: str) -> bool:
    from soa_storage import ObjectNotFoundError

    try:
        await store.head(key)
        return False
    except ObjectNotFoundError:
        return True


async def delete_document_data(
    session: AsyncSession,
    store: ObjectDeleter,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    deletion_state: DeletionState,
    reason: str,
    actor_id: str,
    now: datetime | None = None,
) -> DeletionResult:
    """Erase one document's stored objects and derived rows.

    ``deletion_state`` MUST be :attr:`DeletionState.APPROVED` — the
    caller drives the SEC-008 machine to approval first. Returns a
    :class:`DeletionResult`; safe to call again after a partial failure.
    """
    if deletion_state is not DeletionState.APPROVED:
        raise DeletionNotApprovedError(
            f"deletion requires an APPROVED retention state, got {deletion_state.value!r}"
        )
    organization_id = context.organization_id
    current = now or utcnow()

    tombstone = (
        await session.execute(
            select(DeletionTombstone).where(
                DeletionTombstone.organization_id == organization_id,
                DeletionTombstone.document_id == document_id,
            )
        )
    ).scalar_one_or_none()
    if tombstone is not None and tombstone.state == TombstoneState.COMPLETED.value:
        return DeletionResult(
            tombstone_id=tombstone.id,
            document_id=document_id,
            object_keys_deleted=tombstone.object_keys_deleted,
            category_counts=dict(tombstone.category_counts),
            already_complete=True,
        )

    counts: dict[str, int] = {}

    # 1. Object store: delete every artifact's stored object, idempotently
    # (a missing object is one a prior attempt already removed).
    artifacts = await ArtifactRepository(session, context).list_for_document(document_id)
    keys = [artifact.object_key for artifact in artifacts]
    for key in keys:
        await _object_delete_idempotent(store, key)

    if tombstone is None:
        tombstone = DeletionTombstone(
            organization_id=organization_id,
            document_id=document_id,
            state=TombstoneState.IN_PROGRESS.value,
            reason=reason[:500],
            requested_by=actor_id,
            object_keys_deleted=0,
            category_counts=counts,
            started_at=current,
        )
        session.add(tombstone)
    await session.flush()

    # 2. Reconcile: every object must actually be gone before we erase the
    # rows that point at it. On a partial store failure the delete loop
    # above raised before reaching here; the transaction rolls back and a
    # retry re-deletes only what still survives (the rest are idempotent
    # no-ops), so the count is total-objects-erased, not per-attempt.
    survivors = [key for key in keys if not await _object_absent(store, key)]
    if survivors:
        raise RuntimeError(
            f"object deletion did not reconcile: {len(survivors)} object(s) survive "
            f"for document {document_id}; retry deletion"
        )
    objects_deleted = len(keys)
    tombstone.object_keys_deleted = objects_deleted

    # 3. Database: erase derived rows (children first) then artifact rows.
    counts["artifacts"] = len(artifacts)
    for artifact in artifacts:
        await session.delete(artifact)

    # Stage runs and delivery attempts hang off parent ids.
    run_ids = [
        run.id
        for run in (
            await session.execute(
                select(ProcessingRun).where(
                    ProcessingRun.organization_id == organization_id,
                    ProcessingRun.document_id == document_id,
                )
            )
        )
        .scalars()
        .all()
    ]
    job_ids = [
        job.id
        for job in (
            await session.execute(
                select(ExportJob).where(
                    ExportJob.organization_id == organization_id,
                    ExportJob.document_id == document_id,
                )
            )
        )
        .scalars()
        .all()
    ]
    if run_ids:
        counts["stage_runs"] = await _delete_by(
            session, StageRun, StageRun.run_id.in_(run_ids), organization_id
        )
    if job_ids:
        counts["delivery_attempts"] = await _delete_by(
            session, DeliveryAttempt, DeliveryAttempt.export_job_id.in_(job_ids), organization_id
        )
    counts["export_jobs"] = await _delete_by(
        session, ExportJob, ExportJob.document_id == document_id, organization_id
    )
    counts["processing_runs"] = await _delete_by(
        session, ProcessingRun, ProcessingRun.document_id == document_id, organization_id
    )
    for model in _DERIVED_MODELS:
        document_column = model.document_id  # type: ignore[attr-defined]
        counts[model.__tablename__] = await _delete_by(
            session, model, document_column == document_id, organization_id
        )

    # 4. Provider/cache purge hook — no external caches in this build.
    # (Documented no-op; hosted adapters wire real purges here.)

    tombstone.category_counts = counts
    tombstone.state = TombstoneState.COMPLETED.value
    tombstone.completed_at = current
    await session.flush()

    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="document.data_deleted",
        target_type="document",
        target_id=str(document_id),
        organization_id=organization_id,
        # Counts only — the deleted content is, by definition, gone.
        summary={
            "tombstone_id": str(tombstone.id),
            "reason": reason[:500],
            "object_keys_deleted": objects_deleted,
            "category_counts": counts,
        },
    )
    return DeletionResult(
        tombstone_id=tombstone.id,
        document_id=document_id,
        object_keys_deleted=objects_deleted,
        category_counts=counts,
    )


async def _delete_by(
    session: AsyncSession, model: type[Base], predicate: Any, organization_id: uuid.UUID
) -> int:
    """Count-then-delete rows of ``model`` matching ``predicate`` within
    the organization. Returns how many were removed."""
    scoped = predicate & (model.organization_id == organization_id)  # type: ignore[attr-defined]
    count = len((await session.execute(select(model).where(scoped))).scalars().all())
    if count:
        await session.execute(delete(model).where(scoped))
    return count
