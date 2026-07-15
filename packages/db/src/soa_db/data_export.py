"""Customer data export collector (SEC-009).

Gathers everything the platform holds for ONE document into a single,
category-organized bundle a customer can be handed — the portability /
subject-access unit. It COLLECTS and serializes; the API surface
(SEC-009 router) writes the bundle to object storage, hands out expiring
signed URLs, and audits the export. The deletion counterpart is SEC-010.

Design:

- **documented categories.** :data:`EXPORT_CATEGORIES` is the closed,
  self-describing list of what a document's export contains — each entry
  names the category, a human description, and how to collect it. The
  manifest lists EVERY category with its record count, including empty
  ones, so the export honestly states what exists and what does not.
- **tenant-scoped.** Every collector filters by BOTH ``organization_id``
  and the document, so an export can never reach across tenants even if
  called with a foreign document id (it simply collects nothing).
- **metadata, not file bytes.** Artifacts are exported as METADATA
  (keys, hashes, sizes, retention class) — the original/derived file
  bytes are delivered through the existing signed-download path
  (STO-004), not copied into the bundle. The manifest says so.
- **generic, faithful serialization.** Rows serialize straight from the
  mapped columns (UUIDs and datetimes stringified), so a new column is
  exported automatically and nothing is hand-transcribed out of sync.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import false, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.artifacts import Artifact
from soa_db.audit import AuditEvent
from soa_db.base import Base
from soa_db.canonical_payloads import CanonicalPayload
from soa_db.catalog_selections import CatalogFieldSelection
from soa_db.corrections import FieldCorrection
from soa_db.documents import Document
from soa_db.exports import DeliveryAttempt, ExportJob
from soa_db.extracted_fields import ExtractedField
from soa_db.pages import DocumentPage
from soa_db.review_comments import ReviewComment
from soa_db.review_tasks import ReviewTask
from soa_db.runs import ProcessingRun, StageRun

__all__ = [
    "EXPORT_CATEGORIES",
    "DataCategory",
    "DocumentExport",
    "collect_document_export",
]


def _coerce(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _row_to_dict(row: Base) -> dict[str, Any]:
    """Serialize a mapped row from its columns — JSON-safe, faithful,
    and automatically current with the schema."""
    mapper = inspect(row).mapper
    return {column.key: _coerce(getattr(row, column.key)) for column in mapper.column_attrs}


@dataclass(frozen=True)
class DataCategory:
    key: str
    description: str


#: The closed, self-describing catalogue of what a document export holds.
#: Order is the manifest order (identity first, derivative data after).
EXPORT_CATEGORIES: tuple[DataCategory, ...] = (
    DataCategory("document", "Document metadata, source channel, and current state."),
    DataCategory("processing_runs", "Each processing run over the document."),
    DataCategory("stage_runs", "Per-stage execution records for those runs."),
    DataCategory("pages", "Rendered page metadata (dimensions, dpi) — not the images."),
    DataCategory("extracted_fields", "Extracted field values with confidence and evidence."),
    DataCategory("field_corrections", "Human corrections applied during review."),
    DataCategory(
        "catalog_field_selections",
        "Exact catalog/version/record identities selected for matched fields.",
    ),
    DataCategory(
        "canonical_payloads", "Canonical sales-order payloads produced from the document."
    ),
    DataCategory("review_tasks", "Review tasks raised for the document."),
    DataCategory("review_comments", "Comments and escalations on those review tasks."),
    DataCategory("export_jobs", "Export/delivery intents for the document."),
    DataCategory("delivery_attempts", "Individual outbound delivery attempts."),
    DataCategory(
        "artifacts",
        "Stored artifact METADATA (object key, sha256, size, retention class). "
        "File bytes are fetched via signed download URLs, not included here.",
    ),
    DataCategory("audit_events", "Audit-trail events targeting the document."),
)


@dataclass(frozen=True)
class DocumentExport:
    """A collected export: category key -> list of serialized records."""

    document_id: uuid.UUID
    organization_id: uuid.UUID
    records: dict[str, list[dict[str, Any]]]

    @property
    def total_records(self) -> int:
        return sum(len(rows) for rows in self.records.values())

    def counts(self) -> dict[str, int]:
        return {key: len(self.records.get(key, [])) for key in (c.key for c in EXPORT_CATEGORIES)}


async def _rows(session: AsyncSession, stmt: Any) -> list[dict[str, Any]]:
    return [_row_to_dict(row) for row in (await session.execute(stmt)).scalars().all()]


async def collect_document_export(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    snapshot_at: datetime | None = None,
) -> DocumentExport:
    """Collect every category for one document under its organization.

    Returns an empty-but-well-formed export (all categories present, all
    empty) when the document does not exist in the organization — the
    caller decides whether that is a 404; the collector never leaks
    across tenants."""

    def scoped(model: Any) -> Any:
        stmt = select(model).where(
            model.organization_id == organization_id, model.document_id == document_id
        )
        if snapshot_at is not None:
            stmt = stmt.where(model.created_at <= snapshot_at)
        return stmt

    # Runs first: stage runs and delivery attempts hang off their ids.
    run_rows = (
        (await session.execute(scoped(ProcessingRun).order_by(ProcessingRun.run_number)))
        .scalars()
        .all()
    )
    run_ids: Sequence[uuid.UUID] = [run.id for run in run_rows]
    export_job_rows = (await session.execute(scoped(ExportJob))).scalars().all()
    export_job_ids: Sequence[uuid.UUID] = [job.id for job in export_job_rows]

    stage_stmt = select(StageRun).where(StageRun.organization_id == organization_id)
    stage_stmt = (
        stage_stmt.where(StageRun.run_id.in_(run_ids)) if run_ids else stage_stmt.where(false())
    )
    if snapshot_at is not None:
        stage_stmt = stage_stmt.where(StageRun.created_at <= snapshot_at)
    attempt_stmt = select(DeliveryAttempt).where(DeliveryAttempt.organization_id == organization_id)
    attempt_stmt = (
        attempt_stmt.where(DeliveryAttempt.export_job_id.in_(export_job_ids))
        if export_job_ids
        else attempt_stmt.where(false())
    )
    if snapshot_at is not None:
        attempt_stmt = attempt_stmt.where(DeliveryAttempt.created_at <= snapshot_at)
    audit_stmt = (
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == organization_id,
            AuditEvent.target_type == "document",
            AuditEvent.target_id == str(document_id),
        )
        .order_by(AuditEvent.occurred_at, AuditEvent.id)
    )
    if snapshot_at is not None:
        audit_stmt = audit_stmt.where(AuditEvent.occurred_at <= snapshot_at)
    document_stmt = select(Document).where(
        Document.organization_id == organization_id, Document.id == document_id
    )
    if snapshot_at is not None:
        document_stmt = document_stmt.where(Document.created_at <= snapshot_at)

    records: dict[str, list[dict[str, Any]]] = {
        "document": await _rows(session, document_stmt),
        "processing_runs": [_row_to_dict(run) for run in run_rows],
        "stage_runs": await _rows(session, stage_stmt),
        "pages": await _rows(session, scoped(DocumentPage).order_by(DocumentPage.page_number)),
        "extracted_fields": await _rows(session, scoped(ExtractedField)),
        "field_corrections": await _rows(session, scoped(FieldCorrection)),
        "catalog_field_selections": await _rows(session, scoped(CatalogFieldSelection)),
        "canonical_payloads": await _rows(session, scoped(CanonicalPayload)),
        "review_tasks": await _rows(session, scoped(ReviewTask)),
        "review_comments": await _rows(session, scoped(ReviewComment)),
        "export_jobs": [_row_to_dict(job) for job in export_job_rows],
        "delivery_attempts": await _rows(session, attempt_stmt),
        "artifacts": await _rows(session, scoped(Artifact)),
        "audit_events": await _rows(session, audit_stmt),
    }
    return DocumentExport(document_id=document_id, organization_id=organization_id, records=records)
