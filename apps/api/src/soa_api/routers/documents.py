"""Documents queue API (ING-009).

Cursor pagination over the tenant's documents with filters (state,
stream, source channel, text search), explicit sorts, and field
projection. The cursor is BOUND to its query: it encodes the last row id
plus a fingerprint of (organization, filters, sort), so a cursor minted
for one tenant or one filter combination is rejected with 400 when
replayed against another — cursors cannot cross tenants or incompatible
filters, they can only continue the exact listing that produced them.
"""

import base64
import binascii
import copy
import hashlib
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, get_dependencies
from soa_api.domain.streams import StreamRepository, StreamVersionRepository
from soa_api.services.runtime_pins import RuntimePinError, resolve_runtime_pins
from soa_db.advisory import transaction_advisory_lock
from soa_db.artifacts import ArtifactRepository
from soa_db.audit import ActorType, AuditEvent, record_audit_event
from soa_db.canonical_payloads import CanonicalPayloadRepository
from soa_db.data_deletion import DeletionTombstone, TombstoneState
from soa_db.deletion_requests import (
    DOCUMENT_DELETION_LIFECYCLE_LOCK,
    DeletionRequestRepository,
    DeletionRequestState,
)
from soa_db.documents import (
    Document,
    DocumentRepository,
    DocumentState,
    InvalidDocumentTransitionError,
    transition_document,
)
from soa_db.jobs import enqueue_job
from soa_db.pages import DocumentPageRepository
from soa_db.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from soa_db.review_tasks import cancel_active_task_for_document
from soa_db.runs import ProcessingRun, ProcessingRunRepository, StageRunRepository

router = APIRouter(tags=["documents"])

SORTS = ("received_desc", "received_asc", "priority")

#: Full projection; ``fields`` selects a subset.
DOCUMENT_FIELDS = (
    "id",
    "stream_id",
    "state",
    "state_reason",
    "source_channel",
    "original_filename",
    "content_sha256",
    "size_bytes",
    "content_type",
    "client_reference",
    "priority",
    "sla_due_at",
    "received_at",
    "duplicate_of",
)


def _fingerprint(organization_id: uuid.UUID, filters: dict[str, str | None]) -> str:
    material = f"{organization_id}|" + "|".join(
        f"{key}={filters[key] or ''}" for key in sorted(filters)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def encode_documents_cursor(
    last_id: uuid.UUID, organization_id: uuid.UUID, filters: dict[str, str | None]
) -> str:
    raw = f"{last_id}:{_fingerprint(organization_id, filters)}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def decode_documents_cursor(
    cursor: str, organization_id: uuid.UUID, filters: dict[str, str | None]
) -> uuid.UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        last_id_raw, fingerprint = raw.rsplit(":", 1)
        last_id = uuid.UUID(last_id_raw)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed cursor."
        ) from exc
    if fingerprint != _fingerprint(organization_id, filters):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This cursor belongs to a different listing; restart from the first page.",
        )
    return last_id


def _project(document: Document, fields: tuple[str, ...]) -> dict[str, Any]:
    deleted_values: dict[str, Any] = {
        "id": document.id,
        "stream_id": "",
        "state": DocumentState.DELETED.value,
        "state_reason": "document data deleted",
        "source_channel": "",
        "original_filename": "[deleted]",
        "content_sha256": "",
        "size_bytes": 0,
        "content_type": "application/octet-stream",
        "client_reference": None,
        "priority": 0,
        "sla_due_at": None,
        # The erasure transition updates the shell; use that timestamp rather
        # than retransmitting the original intake time in an explicit deleted
        # listing.
        "received_at": document.updated_at,
        "duplicate_of": None,
    }
    row: dict[str, Any] = {}
    for field in fields:
        value = (
            deleted_values[field]
            if document.state == DocumentState.DELETED.value
            else getattr(document, field)
        )
        if isinstance(value, uuid.UUID):
            row[field] = str(value)
        elif hasattr(value, "isoformat"):
            row[field] = value.isoformat()
        else:
            row[field] = value
    return row


@router.get("/orgs/{organization_slug}/documents")
async def list_documents(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
    document_state: Annotated[str | None, Query()] = None,
    stream: Annotated[str | None, Query(description="Stream slug")] = None,
    source_channel: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str, Query()] = "received_desc",
    fields: Annotated[str | None, Query(description="Comma-separated projection")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    if sort not in SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown sort {sort!r}; one of {', '.join(SORTS)}.",
        )
    if document_state is not None and document_state not in {s.value for s in DocumentState}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown state {document_state!r}."
        )
    projection: tuple[str, ...] = DOCUMENT_FIELDS
    if fields is not None:
        requested = tuple(f.strip() for f in fields.split(",") if f.strip())
        unknown = [f for f in requested if f not in DOCUMENT_FIELDS]
        if unknown or not requested:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown fields: {', '.join(unknown) or '(none requested)'}.",
            )
        projection = requested

    stream_id: uuid.UUID | None = None
    if stream is not None:
        stream_record = await StreamRepository(session, authorized.org_context).get_by_slug(stream)
        if stream_record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found.")
        stream_id = stream_record.id

    filters: dict[str, str | None] = {
        "state": document_state,
        "stream": stream,
        "source_channel": source_channel,
        "search": search,
        "sort": sort,
    }
    organization_id = authorized.org_context.organization_id

    stmt = select(Document).where(Document.organization_id == organization_id)
    if document_state is not None:
        stmt = stmt.where(Document.state == document_state)
    else:
        # Deleted documents are a tombstone, not active work (SEC-010): an
        # unfiltered list hides them, same as any soft-delete convention.
        # ?document_state=deleted (or the UI's "Deleted" filter) still
        # finds them — nothing is unreachable, just off the default view.
        stmt = stmt.where(Document.state != DocumentState.DELETED.value)
    if stream_id is not None:
        stmt = stmt.where(Document.stream_id == stream_id)
    if source_channel is not None:
        stmt = stmt.where(Document.source_channel == source_channel)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Document.original_filename.ilike(pattern),
                Document.client_reference == search,
            )
        )

    # uuid7 primary keys are time-ordered, so id-order IS received-order
    # and the PK index serves the sort.
    if sort == "received_asc":
        stmt = stmt.order_by(Document.id.asc())
    elif sort == "priority":
        stmt = stmt.order_by(Document.priority.asc(), Document.id.desc())
    else:
        stmt = stmt.order_by(Document.id.desc())

    if cursor is not None:
        last_id = decode_documents_cursor(cursor, organization_id, filters)
        if sort == "received_asc":
            stmt = stmt.where(Document.id > last_id)
        elif sort == "priority":
            # Composite keyset for the priority sort would need (priority,
            # id); a plain id keyset is wrong there, so refuse the combo
            # honestly instead of paginating incorrectly.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The priority sort does not support cursors yet; use received order.",
            )
        else:
            stmt = stmt.where(Document.id < last_id)

    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        encode_documents_cursor(items[-1].id, organization_id, filters)
        if has_more and items and sort != "priority"
        else None
    )
    return {
        "items": [_project(document, projection) for document in items],
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/orgs/{organization_slug}/documents/{document_id}/cancel")
async def cancel_document(
    document_id: uuid.UUID,
    body: CancelRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    """Cancel an active document. The state machine decides validity: a
    settled document (completed/rejected/archived/…) answers 409."""
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    try:
        await transition_document(
            session,
            authorized.org_context,
            document=document,
            to_state=DocumentState.CANCELLED,
            reason=body.reason,
            actor_id=f"user:{authorized.membership.user_id}",
            actor_type=ActorType.USER,
        )
    except InvalidDocumentTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    # A cancelled document has no reviewable work left (REV-001).
    await cancel_active_task_for_document(
        session,
        authorized.org_context,
        document_id=document.id,
        reason="document cancelled",
        actor_id=f"user:{authorized.membership.user_id}",
    )
    return {"id": str(document.id), "state": document.state}


@router.get("/orgs/{organization_slug}/documents/{document_id}/runs")
async def list_document_runs(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Processing runs with their stage attempts (PRC-014). Errors are the
    stored SAFE strings — raw provider responses are never persisted, so
    they cannot appear here; stage output summaries pass the same
    redaction as the audit timeline."""
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    runs = await ProcessingRunRepository(session, authorized.org_context).list_for_document(
        document.id
    )
    stage_repo = StageRunRepository(session, authorized.org_context)
    stages_by_run = await stage_repo.list_for_runs([run.id for run in runs])
    payload: list[dict[str, Any]] = []
    for run in runs:
        stages = stages_by_run[run.id]
        payload.append(
            {
                "id": str(run.id),
                "run_number": run.run_number,
                "state": run.state,
                "triggered_by": run.triggered_by,
                "stream_version_id": (
                    str(run.stream_version_id) if run.stream_version_id else None
                ),
                "config_fingerprint": run.config_fingerprint,
                "contract_fingerprint": run.execution_fingerprint,
                "runtime_fingerprint": run.runtime_fingerprint,
                "runtime_provenance": run.runtime_provenance,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "total_latency_ms": run.total_latency_ms,
                "total_cost_cents": run.total_cost_cents,
                "stages": [
                    {
                        "stage": stage.stage,
                        "attempt": stage.attempt,
                        "state": stage.state,
                        "provider": stage.provider,
                        "latency_ms": stage.latency_ms,
                        "cost_cents": stage.cost_cents,
                        "safe_error": stage.safe_error,
                        "failure_class": stage.failure_class,
                        "output_summary": _redact_summary(stage.output_summary),
                        "started_at": stage.started_at.isoformat(),
                        "finished_at": (
                            stage.finished_at.isoformat() if stage.finished_at else None
                        ),
                    }
                    for stage in stages
                ],
            }
        )
    return {"document_id": str(document.id), "state": document.state, "runs": payload}


@router.get("/orgs/{organization_slug}/documents/{document_id}/pages")
async def list_document_pages(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Rendered pages of the document's most recent run that produced
    any (REV-004 viewer). Page images and text artifacts are referenced
    by ARTIFACT ID — the client exchanges those for short-lived signed
    URLs via the STO-004 endpoint; object keys never appear here."""
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    runs = await ProcessingRunRepository(session, authorized.org_context).list_for_document(
        document.id
    )
    page_repo = DocumentPageRepository(session, authorized.org_context)
    for run in reversed(runs):
        pages = await page_repo.list_for_run(run.id)
        if pages:
            return {
                "document_id": str(document.id),
                "run_id": str(run.id),
                "run_number": run.run_number,
                "pages": [
                    {
                        "page_number": page.page_number,
                        "width_px": page.width_px,
                        "height_px": page.height_px,
                        "dpi": page.dpi,
                        "rotation_degrees": page.rotation_degrees,
                        "content_type": page.content_type,
                        "image_artifact_id": str(page.image_artifact_id),
                        "text_artifact_id": (
                            str(page.text_artifact_id) if page.text_artifact_id else None
                        ),
                    }
                    for page in pages
                ],
            }
    return {"document_id": str(document.id), "run_id": None, "run_number": None, "pages": []}


#: States where the record is (or is becoming) a business commitment —
#: reprocessing them is refused by POLICY, not just by the state machine,
#: and the response says so explicitly.
_REPROCESS_PROTECTED = {
    DocumentState.APPROVED.value: "the document is approved",
    DocumentState.EXPORTING.value: "the document is being exported",
    DocumentState.COMPLETED.value: "the document has been exported",
    DocumentState.ARCHIVED.value: "the document is archived",
    DocumentState.DELETED.value: "the document's data has been deleted",
}


class ReprocessRequest(BaseModel):
    #: retry = same configuration as the last run; current_config = the
    #: stream's currently published configuration; historical_config = the
    #: configuration a specific earlier run used (extra authority required).
    mode: Literal["retry", "current_config", "historical_config"] = "current_config"
    run_id: uuid.UUID | None = None  # historical_config only
    reason: str = Field(min_length=3, max_length=500)


def _processing_run_pin_payload(run: ProcessingRun) -> dict[str, str | None]:
    """Recreate the exact immutable job contract retained by a prior run."""

    if (
        run.stream_version_id is None
        or run.config_fingerprint is None
        or run.provider_policy_version_id is None
        or run.execution_fingerprint is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The selected run predates the complete immutable execution contract; "
                "reprocess with the current published configuration instead."
            ),
        )
    return {
        "stream_version_id": str(run.stream_version_id),
        "config_fingerprint": run.config_fingerprint,
        "instruction_version_id": (
            str(run.instruction_version_id) if run.instruction_version_id else None
        ),
        "confidence_policy_version_id": (
            str(run.confidence_policy_version_id) if run.confidence_policy_version_id else None
        ),
        "provider_policy_version_id": str(run.provider_policy_version_id),
        "provider_credential_ref": run.provider_credential_ref,
        "execution_fingerprint": run.execution_fingerprint,
    }


@router.post("/orgs/{organization_slug}/documents/{document_id}/reprocess")
async def reprocess_document(
    document_id: uuid.UUID,
    body: ReprocessRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.reprocess"))],
    session: DbSession,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    """Start a NEW processing run for the document (PRC-013).

    The previous runs and their artifacts stay immutable evidence; the
    document re-enters the queue and the full pipeline re-executes under
    the pinned configuration the chosen mode selects. Approved, exporting,
    completed, and archived documents are protected by policy."""
    # Abuse control (SEC-003): per-principal cap on reprocessing.
    await deps.rate_limiter.enforce(
        "reprocess",
        f"user:{authorized.membership.user_id}",
        deps.settings.rate_limit_reprocess_per_minute,
    )
    await transaction_advisory_lock(
        session,
        DOCUMENT_DELETION_LIFECYCLE_LOCK,
        authorized.org_context.organization_id,
        document_id,
    )
    document = await DocumentRepository(session, authorized.org_context).get(
        document_id, for_update=True
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    deletion_request = await DeletionRequestRepository(
        session, authorized.org_context
    ).get_for_document(document_id, for_update=True)
    if (
        deletion_request is not None
        and deletion_request.state != DeletionRequestState.CANCELLED.value
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Reprocessing is not allowed while the document has a "
                f"{deletion_request.state!r} deletion request."
            ),
        )
    if document.state in _REPROCESS_PROTECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Reprocessing is not allowed: {_REPROCESS_PROTECTED[document.state]}. "
                "Approved and exported records are protected by policy."
            ),
        )

    runs = await ProcessingRunRepository(session, authorized.org_context).list_for_document(
        document.id
    )
    if body.mode == "retry":
        if not runs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The document has never run; there is no configuration to retry under.",
            )
        source = runs[-1]
        runtime_pin_payload = _processing_run_pin_payload(source)
        stream_version_id = source.stream_version_id
        config_fingerprint = source.config_fingerprint
        consequence = (
            f"A new run will re-execute the full pipeline under the same configuration "
            f"as run {source.run_number}."
        )
    elif body.mode == "historical_config":
        # Re-running under an OLD configuration is a config-authority
        # decision, not a routine operator action.
        if "streams.manage" not in authorized.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Historical-configuration runs require the streams.manage permission.",
            )
        if body.run_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="historical_config mode requires run_id.",
            )
        historical = next((run for run in runs if run.id == body.run_id), None)
        if historical is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="That run does not exist for this document.",
            )
        runtime_pin_payload = _processing_run_pin_payload(historical)
        stream_version_id = historical.stream_version_id
        config_fingerprint = historical.config_fingerprint
        consequence = (
            f"A new run will re-execute the full pipeline under the HISTORICAL "
            f"configuration of run {historical.run_number}, not the stream's current one."
        )
    else:  # current_config
        stream = await StreamRepository(session, authorized.org_context).get(document.stream_id)
        try:
            pins = await resolve_runtime_pins(
                session,
                authorized.org_context,
                stream_version_id=stream.active_version_id if stream is not None else None,
            )
        except RuntimePinError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        runtime_pin_payload = pins.job_payload()
        stream_version_id = pins.stream_version_id
        config_fingerprint = pins.config_fingerprint
        consequence = (
            "A new run will re-execute the full pipeline under the stream's currently "
            "published configuration."
        )

    try:
        await transition_document(
            session,
            authorized.org_context,
            document=document,
            to_state=DocumentState.QUEUED,
            reason=body.reason,
            actor_id=f"user:{authorized.membership.user_id}",
            actor_type=ActorType.USER,
        )
    except InvalidDocumentTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    # The old run's review task (if any) is stale the moment a new run is
    # requested; the new run routes to review again if it needs to.
    await cancel_active_task_for_document(
        session,
        authorized.org_context,
        document_id=document.id,
        reason="document sent back for reprocessing",
        actor_id=f"user:{authorized.membership.user_id}",
    )

    next_run_number = len(runs) + 1
    await enqueue_job(
        session,
        job_type="document.preprocess",
        payload={
            "document_id": str(document.id),
            "stream_id": str(document.stream_id),
            "organization_id": str(authorized.org_context.organization_id),
            **runtime_pin_payload,
        },
        organization_id=authorized.org_context.organization_id,
        # The intake enqueue used the bare document key; each reprocess is
        # its own idempotent intent.
        dedupe_key=f"document.preprocess:{document.id}:run:{next_run_number}",
        priority=document.priority,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=f"user:{authorized.membership.user_id}",
        action="document.reprocess_requested",
        target_type="document",
        target_id=str(document.id),
        organization_id=authorized.org_context.organization_id,
        summary={
            "mode": body.mode,
            "reason": body.reason,
            "run_number": next_run_number,
            "stream_version_id": str(stream_version_id) if stream_version_id else None,
            "config_fingerprint": config_fingerprint,
            "execution_fingerprint": runtime_pin_payload["execution_fingerprint"],
        },
    )
    return {
        "id": str(document.id),
        "state": document.state,
        "mode": body.mode,
        "run_number": next_run_number,
        "pinned": {
            "stream_version_id": str(stream_version_id) if stream_version_id else None,
            "config_fingerprint": config_fingerprint,
            "execution_fingerprint": runtime_pin_payload["execution_fingerprint"],
        },
        "consequence": consequence
        + " Previous runs and their artifacts remain unchanged as evidence.",
    }


#: Timeline summaries may reference stored objects only by hash, never by
#: key — object keys are server-side secrets (STO-004). This is defense in
#: depth for audit summaries written by future stages.
_REDACTED_SUMMARY_KEYS = frozenset({"object_key", "upload_url", "signed_url"})


def _redact_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {}
    return {key: value for key, value in summary.items() if key not in _REDACTED_SUMMARY_KEYS}


async def _deleted_document_tombstone(
    session: DbSession,
    authorized: AuthorizedContext,
    document: Document,
) -> dict[str, Any]:
    """Return deletion evidence without retransmitting retained history.

    The anonymized shell, audit rows, request, and legal-hold history remain
    available to their dedicated privileged ledgers. The general document
    detail endpoint exposes only the stable identifier plus counts-only proof,
    so neither a browser cache nor an intermediary receives historical stream,
    actor, reason, filename, or timeline data after erasure.
    """

    tombstone = (
        await session.execute(
            select(DeletionTombstone).where(
                DeletionTombstone.organization_id == authorized.org_context.organization_id,
                DeletionTombstone.document_id == document.id,
                DeletionTombstone.state == TombstoneState.COMPLETED.value,
            )
        )
    ).scalar_one_or_none()
    completed_at = tombstone.completed_at if tombstone is not None else None
    return {
        "document": {
            "id": str(document.id),
            "stream_id": "",
            "state": DocumentState.DELETED.value,
            "state_reason": "document data deleted",
            "source_channel": "",
            "original_filename": "[deleted]",
            "content_sha256": "",
            "size_bytes": 0,
            "content_type": "application/octet-stream",
            "client_reference": None,
            "priority": 0,
            "sla_due_at": None,
            "received_at": completed_at.isoformat() if completed_at is not None else "",
            "duplicate_of": None,
        },
        "artifacts": [],
        "context": {"stream_id": ""},
        "timeline": [],
        "deletion_tombstone": {
            "completed_at": completed_at.isoformat() if completed_at is not None else None,
            "object_keys_deleted": (
                tombstone.object_keys_deleted if tombstone is not None else None
            ),
            "category_counts": dict(tombstone.category_counts) if tombstone is not None else {},
        },
    }


def _redact_canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The read-only projection (CAN-004): callers without
    ``documents.review`` get the business payload with internal notes
    and reviewer identities removed — redaction happens SERVER-side, so
    withheld content never reaches the client at all."""
    redacted = copy.deepcopy(payload)
    redacted["notes"] = [
        note
        for note in redacted.get("notes", [])
        if isinstance(note, dict) and note.get("visibility") == "external"
    ]
    for item in redacted.get("line_items", []):
        if isinstance(item, dict) and "notes" in item:
            item["notes"] = [
                note
                for note in item["notes"]
                if isinstance(note, dict) and note.get("visibility") == "external"
            ]
    for entry in redacted.get("provenance", {}).values():
        if isinstance(entry, dict):
            entry.pop("actor", None)
    return redacted


@router.get("/orgs/{organization_slug}/documents/{document_id}/canonical-payload")
async def get_canonical_payload(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    """The approved canonical order for a document (CAN-004): the latest
    payload, or a specific run's via ``run_id``. Reviewers get the full
    payload and may copy/download; read-only callers get the redacted
    projection. The payload contains business data only — no object
    keys, credentials, or other server-side secrets exist in it."""
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    repo = CanonicalPayloadRepository(session, authorized.org_context)
    row = (
        await repo.latest_for_run(run_id)
        if run_id is not None
        else await repo.latest_for_document(document.id)
    )
    if row is None or row.document_id != document.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No canonical payload exists yet — it is created when the document is approved.",
        )
    full_access = "documents.review" in authorized.permissions
    return {
        "document_id": str(document.id),
        "run_id": str(row.run_id),
        "schema_version": row.schema_version,
        "sha256": row.sha256,
        "created_at": row.created_at.isoformat(),
        "created_by": row.created_by if full_access else None,
        "can_copy": full_access,
        "redacted": not full_access,
        "payload": row.payload if full_access else _redact_canonical_payload(row.payload),
    }


@router.get("/orgs/{organization_slug}/documents/{document_id}")
async def get_document_detail(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
) -> dict[str, Any]:
    """Summary, artifacts, configuration context, and the audit timeline
    (ING-011). Object keys and other server-side internals never appear;
    the timeline is sorted by occurrence with id as the stable tiebreak."""
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    if document.state == DocumentState.DELETED.value:
        return await _deleted_document_tombstone(session, authorized, document)

    artifacts = await ArtifactRepository(session, authorized.org_context).list_for_document(
        document.id
    )
    artifact_rows = [
        {
            "id": str(artifact.id),
            "kind": artifact.kind,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "content_type": artifact.content_type,
            "produced_by_stage": artifact.produced_by_stage,
            "retention_class": artifact.retention_class,
            "created_at": artifact.created_at.isoformat(),
        }
        for artifact in artifacts
    ]

    # Configuration context: the parent stream and what it currently pins.
    stream = await StreamRepository(session, authorized.org_context).get(document.stream_id)
    context: dict[str, Any] = {"stream_id": str(document.stream_id)}
    if stream is not None:
        context["stream_slug"] = stream.slug
        context["stream_name"] = stream.name
        if stream.active_version_id is not None:
            active = await StreamVersionRepository(session, authorized.org_context).get(
                stream.active_version_id
            )
            if active is not None:
                context["stream_version_number"] = active.version_number
                context["pinned_process_version_id"] = (
                    str(active.pinned_process_version_id)
                    if active.pinned_process_version_id
                    else None
                )

    target_ids = [str(document.id), *(str(artifact.id) for artifact in artifacts)]
    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.organization_id == authorized.org_context.organization_id,
                    AuditEvent.target_id.in_(target_ids),
                )
                .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    timeline = [
        {
            "occurred_at": event.occurred_at.isoformat(),
            "action": event.action,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "target_type": event.target_type,
            "summary": _redact_summary(event.summary),
            "correlation_id": event.correlation_id,
        }
        for event in events
    ]

    return {
        "document": _project(document, DOCUMENT_FIELDS),
        "artifacts": artifact_rows,
        "context": context,
        "timeline": timeline,
    }
