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
from soa_db.audit import ActorType, record_audit_event
from soa_db.documents import Document
from soa_db.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from soa_db.review_tasks import (
    InvalidReviewTaskTransitionError,
    ReviewTask,
    ReviewTaskRepository,
    ReviewTaskState,
    release_task,
)
from soa_db.types import utcnow

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
