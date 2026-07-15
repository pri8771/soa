"""Data deletion workflow (SEC-010).

Orchestrates the actual erasure of one document's data once the
retention engine (SEC-008) has ruled it eligible AND a deletion has
been approved. It removes stored objects and derived database rows,
reconciles that the objects are gone, and leaves a :class:`DeletionTombstone`
— the retained proof of what was deleted — plus an audit event. The document
row is reduced to a non-content shell and the audit/tombstone evidence is
deliberately kept so the deletion stays attributable.

Safety properties:

- **gated.** ``delete_document_data`` refuses unless the caller passes
  an ``approved`` deletion state (the SEC-008 machine reached APPROVED).
  It never decides eligibility itself — that is the engine's job.
- **idempotent and retry-safe.** Deletion is driven through a single
  tombstone per document (unique constraint). A missing object or an
  already-deleted row is not an error; re-running after a partial
  failure deletes only what remains and completes the SAME tombstone.
  A store error re-raises; the durable request records a safe failure and a
  retry resumes from the surviving objects. External deletes are idempotent.
- **reconciled.** After deleting objects the workflow re-checks each
  key and fails loudly if anything survives — a deletion that did not
  actually delete is never reported as complete.
- **complete live-data scope.** Content-bearing rows, uploads, gold examples,
  copied export bundles, artifact objects, and queued event payloads are
  erased; financial rows are unlinked and the document shell anonymized.
  Hosted extraction adapters may have transmitted content under the
  tenant's published provider policy, but this workflow has no vendor-side
  purge API and does not claim to erase provider processing caches. Provider
  no-retention terms and regional controls are therefore a launch/vendor
  contract gate, not an automated deletion step. Backup retention is a deliberate,
  documented exception: point-in-time backups age out under their own
  retention and are not individually purged — the tombstone records the
  logical deletion time from which that clock is measured.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import MetaData, String, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.advisory import transaction_advisory_lock
from soa_db.artifacts import Artifact, ArtifactRepository
from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.canonical_payloads import CanonicalPayload
from soa_db.catalog_selections import CatalogFieldSelection
from soa_db.corrections import FieldCorrection
from soa_db.data_export_jobs import DataExportJob, DataExportState
from soa_db.deletion_requests import (
    DELETION_SETTLED_STATES,
    DOCUMENT_DELETION_LIFECYCLE_LOCK,
    LegalHold,
    LegalHoldState,
)
from soa_db.documents import Document, DocumentState, transition_document
from soa_db.evaluation_runs import EvaluationRun, EvaluationRunState
from soa_db.exports import DeliveryAttempt, ExportJob
from soa_db.external_cleanup import (
    ExternalResourceType,
    complete_external_cleanup_intent_for_locator,
)
from soa_db.extracted_fields import ExtractedField
from soa_db.gold_datasets import GoldDocument
from soa_db.jobs import Job, JobStatus
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON, OutboxEvent, OutboxStatus
from soa_db.pages import DocumentPage
from soa_db.repository import OrganizationContext, OrganizationScopedMixin
from soa_db.retention import DeletionState
from soa_db.review_comments import ReviewComment
from soa_db.review_tasks import ReviewTask
from soa_db.runs import ProcessingRun, StageRun
from soa_db.types import GUID, UTCDateTime, utcnow
from soa_db.uploads import UploadSession
from soa_db.usage_ledger import UsageEntry

__all__ = [
    "DOCUMENT_JSON_REFERENCE_POLICY",
    "DOCUMENT_LOCATOR_REFERENCE_POLICY",
    "DOCUMENT_REFERENCE_COLUMN_POLICY",
    "DeletionBlockedError",
    "DeletionNotApprovedError",
    "DeletionResult",
    "DeletionTombstone",
    "ObjectDeleter",
    "TombstoneState",
    "assert_document_reference_policy_complete",
    "delete_document_data",
]


class TombstoneState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DeletionNotApprovedError(Exception):
    """Deletion was requested for a document that has not been approved
    for deletion by the retention state machine (SEC-008)."""


class DeletionBlockedError(RuntimeError):
    """Deletion cannot safely proceed (for example, an active legal hold)."""


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
class ObjectDeleter(Protocol):  # pragma: no cover - structural typing marker
    async def delete(self, key: str) -> None: ...
    async def head(self, key: str) -> Any: ...


#: Derived per-document tables erased on deletion, in FK-safe order
#: (children before parents). document_id-scoped models only; stage
#: runs and delivery attempts are handled via their parent ids.
_DERIVED_MODELS: tuple[type[Base], ...] = (
    ReviewComment,
    ReviewTask,
    CatalogFieldSelection,
    FieldCorrection,
    ExtractedField,
    CanonicalPayload,
    DocumentPage,
)


# Every typed schema column that directly names a document, or names a
# processing run owned by a document, has an explicit erasure policy.  The
# completeness test compares this closed inventory with SQLAlchemy metadata;
# adding a new reference without choosing erase/anonymize/retain breaks CI.
DOCUMENT_REFERENCE_COLUMN_POLICY: dict[tuple[str, str], str] = {
    ("artifacts", "document_id"): "erase",
    ("artifacts", "produced_by_run_id"): "erase",
    ("canonical_payloads", "document_id"): "erase",
    ("canonical_payloads", "run_id"): "erase",
    ("catalog_field_selections", "document_id"): "erase",
    ("catalog_field_selections", "run_id"): "erase",
    ("data_export_jobs", "cursor_document_id"): "anonymize",
    ("data_export_jobs", "document_id"): "anonymize",
    ("deletion_requests", "document_id"): "retain_evidence",
    ("deletion_tombstones", "document_id"): "retain_evidence",
    ("document_pages", "document_id"): "erase",
    ("document_pages", "run_id"): "erase",
    ("export_jobs", "document_id"): "erase",
    ("export_jobs", "run_id"): "erase",
    ("extracted_fields", "document_id"): "erase",
    ("extracted_fields", "run_id"): "erase",
    ("field_corrections", "document_id"): "erase",
    ("field_corrections", "run_id"): "erase",
    ("gold_documents", "source_document_id"): "erase",
    ("legal_holds", "document_id"): "retain_evidence",
    ("processing_runs", "document_id"): "erase",
    ("review_comments", "document_id"): "erase",
    ("review_tasks", "document_id"): "erase",
    ("review_tasks", "run_id"): "erase",
    ("stage_runs", "run_id"): "erase",
    ("upload_sessions", "document_id"): "erase",
    ("usage_ledger_entries", "document_id"): "anonymize",
    ("usage_ledger_entries", "run_id"): "anonymize",
}

# JSON cannot be discovered semantically from a column name, so known payload
# containers that carry document identifiers/hashes are a second closed audit
# inventory. The eraser sanitizes/invalidates each one except append-only audit
# evidence, whose separate retention basis is documented explicitly.
DOCUMENT_JSON_REFERENCE_POLICY: dict[tuple[str, str], str] = {
    ("audit_events", "summary"): "retain_evidence",
    ("data_export_jobs", "parts"): "invalidate_bundle",
    ("evaluation_runs", "attestation"): "invalidate_evaluation",
    ("evaluation_runs", "checkpoint"): "invalidate_evaluation",
    ("evaluation_runs", "predictions"): "invalidate_evaluation",
    ("evaluation_runs", "report"): "invalidate_evaluation",
    ("jobs", "payload"): "sanitize_or_cancel",
    ("outbox_events", "payload"): "sanitize_or_suppress",
}

# Opaque external locators are not JSON and intentionally do not have a
# document_id column, but an object key may still name a document.  Keep that
# indirect reference in the same closed erasure inventory so it cannot be
# mistaken for harmless operational metadata.
DOCUMENT_LOCATOR_REFERENCE_POLICY: dict[tuple[str, str], str] = {
    ("external_cleanup_intents", "resource_locator"): "complete_and_scrub_after_reconciliation",
}


def assert_document_reference_policy_complete(metadata: MetaData) -> None:
    """Fail when a modeled document/run reference has no erasure policy."""

    relevant_names = {"document_id", "source_document_id", "cursor_document_id", "run_id"}
    discovered = {
        (table.name, column.name)
        for table in metadata.tables.values()
        for column in table.columns
        if column.name in relevant_names or column.name == "produced_by_run_id"
    }
    declared = set(DOCUMENT_REFERENCE_COLUMN_POLICY)
    missing = discovered - declared
    stale = declared - discovered
    if missing or stale:
        raise AssertionError(
            "document deletion reference policy is incomplete; "
            f"missing={sorted(missing)}, stale={sorted(stale)}"
        )
    missing_json_columns = {
        (table_name, column_name)
        for table_name, column_name in DOCUMENT_JSON_REFERENCE_POLICY
        if table_name not in metadata.tables
        or column_name not in metadata.tables[table_name].columns
    }
    if missing_json_columns:
        raise AssertionError(
            "document deletion JSON reference policy contains missing columns: "
            f"{sorted(missing_json_columns)}"
        )
    missing_locator_columns = {
        (table_name, column_name)
        for table_name, column_name in DOCUMENT_LOCATOR_REFERENCE_POLICY
        if table_name not in metadata.tables
        or column_name not in metadata.tables[table_name].columns
    }
    if missing_locator_columns:
        raise AssertionError(
            "document deletion locator policy contains missing columns: "
            f"{sorted(missing_locator_columns)}"
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


def _export_contains_document(job: DataExportJob, document_id: uuid.UUID) -> bool:
    # Organization bundles copy the gold/evaluation/audit control plane before
    # document parts. Those JSON records can carry hashes or source IDs without
    # a typed document_id, so the safe policy is to invalidate every live
    # organization bundle when any included document is erased.
    if job.scope == "organization":
        return True
    if job.document_id == document_id:
        return True
    wanted = str(document_id)
    return any(
        isinstance(part, dict) and str(part.get("document_id", "")) == wanted for part in job.parts
    )


def _job_names_document(job: Job, document_id: uuid.UUID) -> bool:
    return str(job.payload.get("document_id", "")) == str(document_id)


def _safe_object_keys(
    *, organization_id: uuid.UUID, document_id: uuid.UUID, raw_keys: list[str]
) -> list[str]:
    """Deduplicate and tenant-fence every external object mutation."""

    allowed_prefixes = (
        f"orgs/{organization_id}/",
        f"data-exports/{organization_id}/",
    )
    keys = list(dict.fromkeys(raw_keys))
    unexpected = [key for key in keys if not key.startswith(allowed_prefixes)]
    if unexpected:
        raise DeletionBlockedError(
            "document deletion found an object key outside its tenant namespace "
            f"for document {document_id}"
        )
    return keys


async def delete_document_data(
    session: AsyncSession,
    store: ObjectDeleter,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    deletion_state: DeletionState,
    reason: str,
    actor_id: str,
    requested_by: str | None = None,
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
    await transaction_advisory_lock(
        session, DOCUMENT_DELETION_LIFECYCLE_LOCK, organization_id, document_id
    )

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

    if (
        await session.execute(
            select(LegalHold.id).where(
                LegalHold.organization_id == organization_id,
                LegalHold.document_id == document_id,
                LegalHold.state == LegalHoldState.ACTIVE.value,
            )
        )
    ).scalar_one_or_none() is not None:
        raise DeletionBlockedError("an active legal hold blocks document deletion")

    document = (
        await session.execute(
            select(Document)
            .where(
                Document.organization_id == organization_id,
                Document.id == document_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is None:
        raise DeletionBlockedError("document does not exist in the deletion request tenant")
    if document.state not in DELETION_SETTLED_STATES:
        raise DeletionBlockedError(
            "document must remain in a settled terminal state until deletion completes"
        )

    counts: dict[str, int] = {}

    # 1. Inventory every live object that can carry the document: artifacts,
    # its upload declaration, durable export bundles, and the legacy
    # synchronous bundle represented by a cleanup job. Export bundles are
    # capabilities over copied PII, so erasure invalidates them immediately;
    # waiting for their ordinary TTL would violate the deletion boundary.
    artifacts = await ArtifactRepository(session, context).list_for_document(document_id)
    upload_sessions = list(
        (
            await session.execute(
                select(UploadSession).where(
                    UploadSession.organization_id == organization_id,
                    UploadSession.document_id == document_id,
                )
            )
        )
        .scalars()
        .all()
    )
    durable_exports = list(
        (
            await session.execute(
                select(DataExportJob)
                .where(DataExportJob.organization_id == organization_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    affected_exports = [
        job for job in durable_exports if _export_contains_document(job, document_id)
    ]
    queue_jobs = list(
        (await session.execute(select(Job).where(Job.organization_id == organization_id)))
        .scalars()
        .all()
    )
    synchronous_export_prefix = f"data-exports/{organization_id}/{document_id}/"
    synchronous_export_jobs = [
        job
        for job in queue_jobs
        if any(
            isinstance(key, str) and key.startswith(synchronous_export_prefix)
            for key in job.payload.get("object_keys", [])
        )
    ]
    synchronous_export_keys = [
        key
        for job in synchronous_export_jobs
        for key in job.payload.get("object_keys", [])
        if isinstance(key, str) and key.startswith(synchronous_export_prefix)
    ]
    export_keys = [
        str(part["object_key"])
        for job in affected_exports
        for part in job.parts
        if isinstance(part, dict) and isinstance(part.get("object_key"), str)
    ] + [job.manifest_object_key for job in affected_exports if job.manifest_object_key is not None]
    keys = _safe_object_keys(
        organization_id=organization_id,
        document_id=document_id,
        raw_keys=(
            [artifact.object_key for artifact in artifacts]
            + [upload.object_key for upload in upload_sessions]
            + export_keys
            + synchronous_export_keys
        ),
    )
    for key in keys:
        await _object_delete_idempotent(store, key)

    if tombstone is None:
        tombstone = DeletionTombstone(
            organization_id=organization_id,
            document_id=document_id,
            state=TombstoneState.IN_PROGRESS.value,
            reason=reason[:500],
            requested_by=requested_by or actor_id,
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
    # A failed rollback compensation can leave a durable cleanup intent with
    # this same object key.  Once absence is proven, complete that redundant
    # intent and erase its locator so the reconciliation control plane cannot
    # retain a document-bearing path. Export keys use a separate namespace and
    # cannot be staged by the external-cleanup subsystem's tenant validator.
    cleanup_intents_reconciled = 0
    tenant_object_prefix = f"orgs/{organization_id}/"
    for key in keys:
        if key.startswith(
            tenant_object_prefix
        ) and await complete_external_cleanup_intent_for_locator(
            session,
            organization_id=organization_id,
            resource_type=ExternalResourceType.OBJECT,
            resource_locator=key,
        ):
            cleanup_intents_reconciled += 1
    counts["external_cleanup_intents_reconciled"] = cleanup_intents_reconciled
    objects_deleted = len(keys)
    tombstone.object_keys_deleted = objects_deleted

    # 3. Database: erase content-bearing rows, unlink retained financial
    # evidence, invalidate copied export bundles, and anonymize the retained
    # document shell. Stage runs and delivery attempts hang off parent ids.
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

    counts["usage_ledger_entries_anonymized"] = await _update_by(
        session,
        UsageEntry,
        or_(
            UsageEntry.document_id == document_id,
            UsageEntry.run_id.in_(run_ids) if run_ids else UsageEntry.document_id == document_id,
        ),
        organization_id,
        {
            "document_id": None,
            "run_id": None,
            # Idempotency references embed stage-run identifiers; retaining
            # them would preserve the indirect document link.
            "source_reference": None,
        },
    )

    matching_gold_documents = list(
        (
            await session.execute(
                select(GoldDocument).where(
                    GoldDocument.organization_id == organization_id,
                    or_(
                        GoldDocument.source_document_id == document_id,
                        GoldDocument.document_sha256 == document.content_sha256,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    affected_dataset_versions = {row.dataset_version_id for row in matching_gold_documents}
    affected_evaluations = (
        list(
            (
                await session.execute(
                    select(EvaluationRun)
                    .where(
                        EvaluationRun.organization_id == organization_id,
                        EvaluationRun.dataset_version_id.in_(affected_dataset_versions),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if affected_dataset_versions
        else []
    )
    for evaluation in affected_evaluations:
        evaluation.state = EvaluationRunState.CANCELLED.value
        evaluation.predictions = {}
        evaluation.checkpoint = {}
        evaluation.report = None
        evaluation.gate_result = None
        evaluation.attestation = None
        evaluation.safe_error = "invalidated because a source document was deleted"
        evaluation.finished_at = current
    counts["evaluation_runs_invalidated"] = len(affected_evaluations)
    counts["gold_documents"] = await _delete_by(
        session,
        GoldDocument,
        or_(
            GoldDocument.source_document_id == document_id,
            GoldDocument.document_sha256 == document.content_sha256,
        ),
        organization_id,
    )
    counts["upload_sessions"] = await _delete_by(
        session, UploadSession, UploadSession.document_id == document_id, organization_id
    )
    counts["artifacts"] = await _delete_by(
        session,
        Artifact,
        Artifact.document_id == document_id,
        organization_id,
    )

    for export in affected_exports:
        export.parts = []
        export.manifest_object_key = None
        export.document_id = None
        export.cursor_document_id = None
        export.state = DataExportState.EXPIRED.value
        export.safe_error = "invalidated because its source document was deleted"
        export.finished_at = current
    counts["data_export_jobs_invalidated"] = len(affected_exports)

    affected_evaluation_ids = {str(evaluation.id) for evaluation in affected_evaluations}
    document_jobs = list(
        {
            job.id: job
            for job in queue_jobs
            if _job_names_document(job, document_id)
            or job in synchronous_export_jobs
            or (
                job.job_type == "evaluation.run"
                and str(job.payload.get("evaluation_run_id", "")) in affected_evaluation_ids
            )
        }.values()
    )
    cancelled_jobs = 0
    for job in document_jobs:
        if job.status in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
            job.status = JobStatus.CANCELLED.value
            job.finished_at = current
            cancelled_jobs += 1
        job.payload = {
            "organization_id": str(organization_id),
            "document_id": str(document_id),
            "redacted": "document data deleted",
        }
        job.last_error = "cancelled or sanitized because document data was deleted"
    counts["queue_jobs_sanitized"] = len(document_jobs)
    counts["queue_jobs_cancelled"] = cancelled_jobs

    outbox_events = list(
        (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.organization_id == organization_id)
            )
        )
        .scalars()
        .all()
    )
    affected_events = [
        event
        for event in outbox_events
        if str(event.payload.get("document_id", "")) == str(document_id)
    ]
    for event in affected_events:
        event.payload = {
            "document_id": str(document_id),
            "redacted": "document data deleted",
        }
        if event.status == OutboxStatus.PENDING.value:
            event.status = OutboxStatus.FAILED.value
            event.last_error = "publication suppressed because document data was deleted"
    counts["outbox_events_sanitized"] = len(affected_events)

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

    counts["duplicate_links_anonymized"] = await _update_by(
        session,
        Document,
        Document.duplicate_of == document_id,
        organization_id,
        {"duplicate_of": None, "updated_at": current},
    )
    await transition_document(
        session,
        context,
        document=document,
        to_state=DocumentState.DELETED,
        reason="document data deleted",
        actor_id=actor_id,
    )
    counts["document_shell_anonymized"] = await _update_by(
        session,
        Document,
        Document.id == document_id,
        organization_id,
        {
            "original_filename": "[deleted]",
            "content_sha256": "0" * 64,
            "size_bytes": 0,
            "content_type": "application/octet-stream",
            "client_reference": None,
            "source_metadata": {},
            "state_reason": "document data deleted",
            "duplicate_of": None,
            "sla_due_at": None,
            "updated_at": current,
            "version": Document.version + 1,
        },
    )

    # 4. There is no generic vendor-side purge API. Hosted-provider handling
    # is governed by the published tenant policy and vendor contract; do not
    # record an external purge that the platform cannot verify.

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


async def _update_by(
    session: AsyncSession,
    model: type[Base],
    predicate: Any,
    organization_id: uuid.UUID,
    values: dict[str, Any],
) -> int:
    """Bulk-anonymize retained evidence without tripping immutability guards.

    This is intentionally available only inside the deletion module. Usage
    entries remain immutable for ordinary product code; privacy unlinking is
    the narrowly documented exception and never alters billed quantities or
    costs.
    """

    scoped = predicate & (model.organization_id == organization_id)  # type: ignore[attr-defined]
    count = len((await session.execute(select(model).where(scoped))).scalars().all())
    if count:
        await session.execute(update(model).where(scoped).values(**values))
    return count
