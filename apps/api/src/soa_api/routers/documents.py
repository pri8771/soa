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
import hashlib
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.streams import StreamRepository, StreamVersionRepository
from soa_db.artifacts import ArtifactRepository
from soa_db.audit import ActorType, AuditEvent, record_audit_event
from soa_db.documents import (
    Document,
    DocumentRepository,
    DocumentState,
    InvalidDocumentTransitionError,
    transition_document,
)
from soa_db.jobs import enqueue_job
from soa_db.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from soa_db.runs import ProcessingRunRepository, StageRunRepository

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
    row: dict[str, Any] = {}
    for field in fields:
        value = getattr(document, field)
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
    payload: list[dict[str, Any]] = []
    for run in runs:
        stages = await stage_repo.list_for_run(run.id)
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


#: States where the record is (or is becoming) a business commitment —
#: reprocessing them is refused by POLICY, not just by the state machine,
#: and the response says so explicitly.
_REPROCESS_PROTECTED = {
    DocumentState.APPROVED.value: "the document is approved",
    DocumentState.EXPORTING.value: "the document is being exported",
    DocumentState.COMPLETED.value: "the document has been exported",
    DocumentState.ARCHIVED.value: "the document is archived",
}


class ReprocessRequest(BaseModel):
    #: retry = same configuration as the last run; current_config = the
    #: stream's currently published configuration; historical_config = the
    #: configuration a specific earlier run used (extra authority required).
    mode: Literal["retry", "current_config", "historical_config"] = "current_config"
    run_id: uuid.UUID | None = None  # historical_config only
    reason: str = Field(min_length=3, max_length=500)


@router.post("/orgs/{organization_slug}/documents/{document_id}/reprocess")
async def reprocess_document(
    document_id: uuid.UUID,
    body: ReprocessRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.reprocess"))],
    session: DbSession,
) -> dict[str, Any]:
    """Start a NEW processing run for the document (PRC-013).

    The previous runs and their artifacts stay immutable evidence; the
    document re-enters the queue and the full pipeline re-executes under
    the pinned configuration the chosen mode selects. Approved, exporting,
    completed, and archived documents are protected by policy."""
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
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
        stream_version_id = historical.stream_version_id
        config_fingerprint = historical.config_fingerprint
        consequence = (
            f"A new run will re-execute the full pipeline under the HISTORICAL "
            f"configuration of run {historical.run_number}, not the stream's current one."
        )
    else:  # current_config
        stream = await StreamRepository(session, authorized.org_context).get(document.stream_id)
        stream_version_id = stream.active_version_id if stream else None
        config_fingerprint = None
        if stream is not None and stream.active_version_id is not None:
            active = await StreamVersionRepository(session, authorized.org_context).get(
                stream.active_version_id
            )
            if active is not None and active.resolved_snapshot:
                config = active.resolved_snapshot.get("config")
                fingerprint = config.get("fingerprint") if isinstance(config, dict) else None
                config_fingerprint = str(fingerprint) if fingerprint else None
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

    next_run_number = len(runs) + 1
    await enqueue_job(
        session,
        job_type="document.preprocess",
        payload={
            "document_id": str(document.id),
            "stream_id": str(document.stream_id),
            "organization_id": str(authorized.org_context.organization_id),
            "stream_version_id": str(stream_version_id) if stream_version_id else None,
            "config_fingerprint": config_fingerprint,
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
