"""Review queue API (REV-002).

The reviewer's worklist: filtered views over active review tasks
(mine / unassigned / overdue / blocked), atomic claim (a single
conditional UPDATE — two reviewers cannot own the same task), release,
and claim-next for the "start next" flow. Every read and write requires
``documents.review``, and the tenant-scoped repository plus the org-slug
authorization make other tenants' queues invisible, not merely empty.

Cursors follow the ING-009 discipline: bound to (organization, filters,
sort) and honest about limits — priority/SLA orderings refuse cursors
rather than silently skipping rows.
"""

import base64
import binascii
import hashlib
import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, select, update

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.streams import StreamRepository, StreamVersionRepository
from soa_api.services.approval import (
    ApprovalPermissionError,
    ApprovalStateError,
    CriticalBlockersError,
    approve_document,
    reject_document,
)
from soa_api.services.revalidation import normalize_correction, revalidate_run
from soa_db.audit import ActorType, AuditEvent, record_audit_event
from soa_db.corrections import (
    FieldCorrectionRepository,
    latest_corrections,
    record_correction,
)
from soa_db.documents import Document, DocumentRepository
from soa_db.extracted_fields import ExtractedField, ExtractedFieldRepository
from soa_db.pages import DocumentPageRepository
from soa_db.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from soa_db.review_comments import (
    MAX_COMMENT_LENGTH,
    ReviewComment,
    ReviewCommentRepository,
    add_comment,
)
from soa_db.review_tasks import (
    InvalidReviewTaskTransitionError,
    ReviewTask,
    ReviewTaskRepository,
    ReviewTaskState,
    escalate_task,
    release_task,
)
from soa_db.runs import ProcessingRunRepository, StageRunRepository
from soa_db.types import utcnow
from soa_rules.baseline import CANONICAL_FIELD_TYPES

router = APIRouter(tags=["review"])

VIEWS = ("all", "mine", "unassigned", "overdue", "blocked")
SORTS = ("priority", "sla", "created")
_ACTIVE = (ReviewTaskState.OPEN.value, ReviewTaskState.IN_PROGRESS.value)


def _fingerprint(organization_id: uuid.UUID, filters: dict[str, str | None]) -> str:
    material = f"{organization_id}|" + "|".join(
        f"{key}={filters[key] or ''}" for key in sorted(filters)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _encode_cursor(
    last_id: uuid.UUID, organization_id: uuid.UUID, filters: dict[str, str | None]
) -> str:
    raw = f"{last_id}:{_fingerprint(organization_id, filters)}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_cursor(
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


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


def _serialize(task: ReviewTask, document: Document | None) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "document_id": str(task.document_id),
        "run_id": str(task.run_id),
        "state": task.state,
        "priority": task.priority,
        "blocking": task.blocking,
        "sla_due_at": task.sla_due_at.isoformat() if task.sla_due_at else None,
        "assigned_to": task.assigned_to,
        "assigned_at": task.assigned_at.isoformat() if task.assigned_at else None,
        "reasons": task.reasons,
        "outcome": task.outcome,
        "version": task.version,
        "escalated_at": task.escalated_at.isoformat() if task.escalated_at else None,
        "escalated_by": task.escalated_by,
        "escalation_reason": task.escalation_reason,
        "created_at": task.created_at.isoformat(),
        "document_filename": document.original_filename if document else None,
        "document_state": document.state if document else None,
    }


async def _with_documents(
    session: DbSession, authorized: AuthorizedContext, tasks: list[ReviewTask]
) -> list[dict[str, Any]]:
    if not tasks:
        return []
    document_ids = {task.document_id for task in tasks}
    rows = (
        (
            await session.execute(
                select(Document).where(
                    Document.organization_id == authorized.org_context.organization_id,
                    Document.id.in_(document_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    return [_serialize(task, by_id.get(task.document_id)) for task in tasks]


@router.get("/orgs/{organization_slug}/review-tasks")
async def list_review_tasks(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
    view: Annotated[str, Query()] = "all",
    task_state: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "priority",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    if view not in VIEWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown view; expected one of {', '.join(VIEWS)}.",
        )
    if sort not in SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown sort; expected one of {', '.join(SORTS)}.",
        )
    if task_state is not None and task_state not in {s.value for s in ReviewTaskState}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown task state.")

    repo = ReviewTaskRepository(session, authorized.org_context)
    stmt = repo._scoped_select()
    if task_state is not None:
        stmt = stmt.where(ReviewTask.state == task_state)
    else:
        stmt = stmt.where(ReviewTask.state.in_(_ACTIVE))
    if view == "mine":
        stmt = stmt.where(ReviewTask.assigned_to == _actor(authorized))
    elif view == "unassigned":
        stmt = stmt.where(ReviewTask.state == ReviewTaskState.OPEN.value)
    elif view == "overdue":
        stmt = stmt.where(ReviewTask.sla_due_at.is_not(None), ReviewTask.sla_due_at < utcnow())
    elif view == "blocked":
        stmt = stmt.where(ReviewTask.blocking.is_(True))

    filters = {"view": view, "task_state": task_state, "sort": sort}
    organization_id = authorized.org_context.organization_id
    if sort == "priority":
        stmt = stmt.order_by(ReviewTask.priority, ReviewTask.created_at, ReviewTask.id)
    elif sort == "sla":
        stmt = stmt.order_by(ReviewTask.sla_due_at.is_(None), ReviewTask.sla_due_at, ReviewTask.id)
    else:  # created: newest first, keyset-paginated by id
        stmt = stmt.order_by(ReviewTask.id.desc())

    if cursor is not None:
        if sort != "created":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only the created sort supports cursors; restart from the first page.",
            )
        last_id = _decode_cursor(cursor, organization_id, filters)
        stmt = stmt.where(ReviewTask.id < last_id)

    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        _encode_cursor(items[-1].id, organization_id, filters)
        if has_more and items and sort == "created"
        else None
    )
    return {
        "items": await _with_documents(session, authorized, items),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


async def _atomic_claim(
    session: DbSession, authorized: AuthorizedContext, task_id: uuid.UUID
) -> ReviewTask | None:
    """One conditional UPDATE: only an OPEN task moves, so exactly one of
    any number of concurrent claimers wins. Returns the claimed task."""
    actor = _actor(authorized)
    now = utcnow()
    result = await session.execute(
        update(ReviewTask)
        .where(
            ReviewTask.id == task_id,
            ReviewTask.organization_id == authorized.org_context.organization_id,
            ReviewTask.state == ReviewTaskState.OPEN.value,
        )
        .values(
            state=ReviewTaskState.IN_PROGRESS.value,
            assigned_to=actor,
            assigned_at=now,
            updated_at=now,
            version=ReviewTask.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        return None
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor,
        action="review_task.state_changed",
        target_type="review_task",
        target_id=str(task_id),
        organization_id=authorized.org_context.organization_id,
        summary={"from": "open", "to": "in_progress", "assigned_to": actor},
    )
    return task


@router.post("/orgs/{organization_slug}/review-tasks/{task_id}/claim")
async def claim_review_task(
    task_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    claimed = await _atomic_claim(session, authorized, task_id)
    if claimed is not None:
        return _serialize(claimed, None)
    existing = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"The task is {existing.state}"
            + (f", assigned to {existing.assigned_to}" if existing.assigned_to else "")
            + "."
        ),
    )


@router.post("/orgs/{organization_slug}/review-tasks/claim-next")
async def claim_next_review_task(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    """The "start next" flow: claim the highest-priority open task. The
    response says WHY this task is next (priority + age ordering), and
    each candidate is claimed atomically — losing a race just moves on
    to the following candidate."""
    repo = ReviewTaskRepository(session, authorized.org_context)
    candidates = (
        (
            await session.execute(
                repo._scoped_select()
                .where(ReviewTask.state == ReviewTaskState.OPEN.value)
                .order_by(ReviewTask.priority, ReviewTask.created_at, ReviewTask.id)
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        claimed = await _atomic_claim(session, authorized, candidate.id)
        if claimed is not None:
            return {
                "task": _serialize(claimed, None),
                "explanation": (
                    "Highest-priority open task, oldest first within the same priority."
                ),
            }
    return {"task": None, "explanation": "No open tasks to claim."}


class ReleaseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/orgs/{organization_slug}/review-tasks/{task_id}/release")
async def release_review_task(
    task_id: uuid.UUID,
    body: ReleaseRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    if task.assigned_to != _actor(authorized):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assignee may release this task.",
        )
    try:
        await release_task(session, authorized.org_context, task=task, actor_id=_actor(authorized))
    except InvalidReviewTaskTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _serialize(task, None)


#: History returned by the workspace is bounded; older entries live in the
#: full audit trail (ING-011).
_HISTORY_LIMIT = 50


def _field_payload(field: ExtractedField) -> dict[str, Any]:
    """One extracted field for the review workspace: values, provenance,
    validation, candidates, and evidence — by page/polygon/quote only,
    never by object key."""
    return {
        "field_key": field.field_key,
        "row_index": field.row_index,
        "raw_value": field.raw_value,
        "normalized_value": field.normalized_value,
        "normalization_error": field.normalization_error,
        "confidence": field.confidence,
        "validation_status": field.validation_status,
        "provider": field.provider,
        "provider_model": field.provider_model,
        "evidence": [span.to_json() for span in field.evidence_spans()],
        "candidates": [candidate.to_json() for candidate in field.candidate_readings()],
    }


@router.get("/orgs/{organization_slug}/review-tasks/{task_id}/workspace")
async def review_workspace(
    task_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    """Everything the review workspace needs in ONE bounded response
    (REV-006): the task, the document, the run and its route decision,
    header fields and line items with candidates/validations/evidence,
    page metadata for the viewer, recent history, and the configuration
    context. Artifact content stays behind the separate short-lived
    signed-URL endpoint — only artifact IDS appear here."""
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    document = await DocumentRepository(session, authorized.org_context).get(task.document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The task's document is gone."
        )

    run = await ProcessingRunRepository(session, authorized.org_context).get(task.run_id)
    decision: dict[str, Any] | None = None
    if run is not None:
        stages = await StageRunRepository(session, authorized.org_context).list_for_run(run.id)
        for stage in stages:
            if stage.stage == "validating_data" and stage.state == "succeeded":
                summary = stage.output_summary or {}
                raw_decision = summary.get("decision")
                decision = raw_decision if isinstance(raw_decision, dict) else None

    fields = await ExtractedFieldRepository(session, authorized.org_context).list_for_run(
        task.run_id
    )
    corrections = latest_corrections(
        await FieldCorrectionRepository(session, authorized.org_context).list_for_run(task.run_id)
    )
    header = [_field_payload(field) for field in fields if field.row_index is None]
    line_items: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for field in fields:
        if field.row_index is None:
            continue
        table = field.field_key.partition(".")[0]
        line_items.setdefault(table, {}).setdefault(field.row_index, []).append(
            _field_payload(field)
        )
    tables = {
        table: [cells for _, cells in sorted(rows.items())] for table, rows in line_items.items()
    }

    pages = await DocumentPageRepository(session, authorized.org_context).list_for_run(task.run_id)
    page_rows = [
        {
            "page_number": page.page_number,
            "width_px": page.width_px,
            "height_px": page.height_px,
            "dpi": page.dpi,
            "rotation_degrees": page.rotation_degrees,
            "content_type": page.content_type,
            "image_artifact_id": str(page.image_artifact_id),
            "text_artifact_id": str(page.text_artifact_id) if page.text_artifact_id else None,
        }
        for page in pages
    ]

    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.organization_id == authorized.org_context.organization_id,
                    AuditEvent.target_id.in_([str(task.id), str(document.id)]),
                )
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                .limit(_HISTORY_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    history = [
        {
            "occurred_at": event.occurred_at.isoformat(),
            "action": event.action,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "target_type": event.target_type,
            "summary": event.summary or {},
        }
        for event in reversed(events)
    ]

    stream = await StreamRepository(session, authorized.org_context).get(document.stream_id)
    context: dict[str, Any] = {
        "stream_id": str(document.stream_id),
        "run_stream_version_id": str(run.stream_version_id)
        if run and run.stream_version_id
        else None,
        "config_fingerprint": run.config_fingerprint if run else None,
    }
    if stream is not None:
        context["stream_slug"] = stream.slug
        context["stream_name"] = stream.name
        if stream.active_version_id is not None:
            active = await StreamVersionRepository(session, authorized.org_context).get(
                stream.active_version_id
            )
            if active is not None:
                context["current_stream_version_number"] = active.version_number

    return {
        "task": _serialize(task, document),
        "document": {
            "id": str(document.id),
            "state": document.state,
            "state_reason": document.state_reason,
            "original_filename": document.original_filename,
            "priority": document.priority,
            "received_at": document.received_at.isoformat(),
        },
        "run": {
            "id": str(task.run_id),
            "run_number": run.run_number if run else None,
            "state": run.state if run else None,
            "decision": decision,
        },
        "fields": header,
        "line_items": tables,
        "corrections": [
            {
                "field_key": correction.field_key,
                "row_index": correction.row_index,
                "corrected_raw_value": correction.corrected_raw_value,
                "corrected_normalized_value": correction.corrected_normalized_value,
                "normalization_error": correction.normalization_error,
                "corrected_by": correction.corrected_by,
            }
            for correction in corrections.values()
        ],
        "pages": page_rows,
        "history": history,
        "context": context,
    }


class CorrectionRequest(BaseModel):
    field_key: str = Field(min_length=1, max_length=255)
    row_index: int | None = None
    #: The corrected raw value; null/empty clears the field.
    value: str | None = Field(default=None, max_length=10_000)
    reason: str | None = Field(default=None, max_length=500)
    #: Evidence span the reviewer relied on (page/polygon/quote), optional.
    evidence_selection: dict[str, Any] | None = None
    #: The task version this correction was authored against (If-Match).
    expected_version: int


@router.post("/orgs/{organization_slug}/review-tasks/{task_id}/corrections")
async def correct_field(
    task_id: uuid.UUID,
    body: CorrectionRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    """Record a field correction (REV-009): APPEND-ONLY on top of the
    immutable extraction, optimistic-locked on the task version so
    concurrent editors can never silently overwrite each other, and
    followed by a revalidation whose fresh decision comes back in the
    response."""

    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    if task.state != ReviewTaskState.IN_PROGRESS.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The task is {task.state}; claim it before correcting fields.",
        )
    actor = _actor(authorized)
    if task.assigned_to != actor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"The task is assigned to {task.assigned_to}; only the assignee may edit.",
        )
    if body.expected_version != task.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The task changed since you loaded it (server version {task.version}, "
                f"yours {body.expected_version}). Reload before editing — nothing was saved."
            ),
        )

    fields = await ExtractedFieldRepository(session, authorized.org_context).list_for_run(
        task.run_id
    )
    target = next(
        (f for f in fields if f.field_key == body.field_key and f.row_index == body.row_index),
        None,
    )
    # A table cell may target a NEW row (REV-008 add/split): allowed when
    # the column is part of the canonical schema — the correction itself
    # becomes the row's cell. Header fields must exist on the run.
    is_new_table_cell = (
        target is None
        and body.row_index is not None
        and body.row_index >= 0
        and "." in body.field_key
        and CANONICAL_FIELD_TYPES.get(body.field_key) not in (None, "table")
    )
    if target is None and not is_new_table_cell:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That field does not exist on this run.",
        )

    previous = latest_corrections(
        await FieldCorrectionRepository(session, authorized.org_context).list_for_run(task.run_id)
    ).get((body.field_key, body.row_index))
    previous_raw = (
        previous.corrected_raw_value if previous else (target.raw_value if target else None)
    )

    corrected_raw = body.value if body.value is not None and body.value != "" else None
    normalized, normalization_error = normalize_correction(body.field_key, corrected_raw)
    correction = await record_correction(
        session,
        authorized.org_context,
        document_id=task.document_id,
        run_id=task.run_id,
        task_id=task.id,
        field_key=body.field_key,
        row_index=body.row_index,
        previous_raw_value=previous_raw,
        corrected_raw_value=corrected_raw,
        corrected_normalized_value=normalized,
        normalization_error=normalization_error,
        reason=body.reason,
        evidence_selection=body.evidence_selection,
        corrected_by=actor,
        task_version=task.version,
    )
    # Bump the optimistic version: every correction is a task change.
    task.updated_at = utcnow()
    await session.flush()

    document = await DocumentRepository(session, authorized.org_context).get(task.document_id)
    revalidation = (
        await revalidate_run(session, authorized.org_context, document=document, run_id=task.run_id)
        if document is not None
        else None
    )
    return {
        "correction": {
            "id": str(correction.id),
            "field_key": correction.field_key,
            "row_index": correction.row_index,
            "previous_raw_value": correction.previous_raw_value,
            "corrected_raw_value": correction.corrected_raw_value,
            "corrected_normalized_value": correction.corrected_normalized_value,
            "normalization_error": correction.normalization_error,
            "corrected_by": correction.corrected_by,
        },
        "task_version": task.version,
        "revalidation": revalidation,
    }


def _serialize_comment(comment: ReviewComment) -> dict[str, Any]:
    return {
        "id": str(comment.id),
        "task_id": str(comment.task_id),
        "document_id": str(comment.document_id),
        "author": comment.author,
        "body": comment.body,
        "mentions": comment.mentions,
        "created_at": comment.created_at.isoformat(),
    }


@router.get("/orgs/{organization_slug}/review-tasks/{task_id}/comments")
async def list_review_comments(
    task_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    """The comment thread on a review task (REV-011), oldest first. Any
    reviewer in the organization may read and write the thread — commenting
    is collaboration, not editing, so it is not restricted to the assignee."""
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    comments = await ReviewCommentRepository(session, authorized.org_context).list_for_task(task_id)
    return {"items": [_serialize_comment(comment) for comment in comments]}


class CommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_COMMENT_LENGTH)


@router.post(
    "/orgs/{organization_slug}/review-tasks/{task_id}/comments",
    status_code=status.HTTP_201_CREATED,
)
async def add_review_comment(
    task_id: uuid.UUID,
    body: CommentRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    try:
        comment = await add_comment(
            session,
            authorized.org_context,
            task_id=task.id,
            document_id=task.document_id,
            author=_actor(authorized),
            body=body.body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return _serialize_comment(comment)


class ApproveRequest(BaseModel):
    #: Required only when critical blockers remain (authorized override).
    override_reason: str | None = Field(default=None, max_length=500)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


async def _task_and_document(
    session: DbSession, authorized: AuthorizedContext, task_id: uuid.UUID
) -> tuple[ReviewTask, Document]:
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    document = await DocumentRepository(session, authorized.org_context).get(task.document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The task's document is gone."
        )
    return task, document


@router.post("/orgs/{organization_slug}/review-tasks/{task_id}/approve")
async def approve_review_task(
    task_id: uuid.UUID,
    body: ApproveRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.approve"))],
    session: DbSession,
) -> dict[str, Any]:
    """Approve (REV-012): final validation runs NOW; critical blockers
    refuse approval unless an authorized override reason is given;
    remaining warnings are captured on the approval."""
    task, document = await _task_and_document(session, authorized, task_id)
    try:
        result = await approve_document(
            session,
            authorized.org_context,
            task=task,
            document=document,
            actor=_actor(authorized),
            can_override="documents.approve.override" in authorized.permissions,
            override_reason=body.override_reason,
        )
    except CriticalBlockersError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ApprovalStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ApprovalPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
    return {**result, "task": _serialize(task, document)}


@router.post("/orgs/{organization_slug}/review-tasks/{task_id}/reject")
async def reject_review_task(
    task_id: uuid.UUID,
    body: RejectRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.reject"))],
    session: DbSession,
) -> dict[str, Any]:
    task, document = await _task_and_document(session, authorized, task_id)
    try:
        result = await reject_document(
            session,
            authorized.org_context,
            task=task,
            document=document,
            actor=_actor(authorized),
            reason=body.reason,
        )
    except ApprovalStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ApprovalPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return {**result, "task": _serialize(task, document)}


class EscalateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/orgs/{organization_slug}/review-tasks/{task_id}/escalate")
async def escalate_review_task(
    task_id: uuid.UUID,
    body: EscalateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.review"))],
    session: DbSession,
) -> dict[str, Any]:
    """Escalate a task (REV-011): records who raised it and why, bumps
    queue priority, and returns an in-progress task to OPEN so a
    supervisor can claim it. Settled tasks cannot be escalated."""
    task = await ReviewTaskRepository(session, authorized.org_context).get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    try:
        await escalate_task(
            session,
            authorized.org_context,
            task=task,
            reason=body.reason,
            actor_id=_actor(authorized),
        )
    except InvalidReviewTaskTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return _serialize(task, None)
