"""Review-task model and routing (REV-001).

A review task is the unit of human work: one document, one run, one
reason list explaining exactly WHY a human is looking at it (the PRC-011
decision's reasons, each linked to the field/row or rule it came from —
never a bare "needs review").

One ACTIVE primary task per document, enforced two ways: a partial
unique index (open/in_progress rows collapse per document) and the
routing helper, which supersedes any earlier active task before creating
the new one — a reprocessed document gets a fresh task for the fresh
run, and the stale task is cancelled with a reason, not orphaned.

States: open -> in_progress (claim) -> completed, with release back to
open and cancellation from either. Optimistic ``version`` guards
concurrent claims (REV-002 makes the API-level claim atomic on top).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class ReviewTaskState(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


_ACTIVE_STATES = (ReviewTaskState.OPEN.value, ReviewTaskState.IN_PROGRESS.value)

_ALLOWED: dict[str, frozenset[str]] = {
    ReviewTaskState.OPEN.value: frozenset(
        {ReviewTaskState.IN_PROGRESS.value, ReviewTaskState.CANCELLED.value}
    ),
    ReviewTaskState.IN_PROGRESS.value: frozenset(
        {
            ReviewTaskState.OPEN.value,  # release
            ReviewTaskState.COMPLETED.value,
            ReviewTaskState.CANCELLED.value,
        }
    ),
    ReviewTaskState.COMPLETED.value: frozenset(),
    ReviewTaskState.CANCELLED.value: frozenset(),
}


class InvalidReviewTaskTransitionError(Exception):
    def __init__(self, task_id: object, source: str, target: str) -> None:
        super().__init__(
            f"review task {task_id}: transition {source!r} -> {target!r} is not allowed"
        )


class ReviewTask(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "review_tasks"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReviewTaskState.OPEN.value
    )
    #: PRC-011 reasons verbatim: [{code, message, field_key, row_index,
    #: rule_key}] — every entry names its source.
    reasons: Mapped[list[Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)
    priority: Mapped[int] = mapped_column(nullable=False, default=100)
    #: True when a blocking rule fired: the document cannot export until
    #: resolved, so these tasks surface in their own queue view.
    blocking: Mapped[bool] = mapped_column(nullable=False, default=False)
    sla_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: How the task ended: approved/rejected for completions, a safe
    #: reason for cancellations.
    outcome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Escalation (REV-011): why, who raised it, and when. An escalated
    #: task returns to OPEN so a supervisor can take ownership.
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    escalated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        # One ACTIVE primary task per document.
        Index(
            "uq_review_tasks_single_active",
            "document_id",
            unique=True,
            postgresql_where=text("state IN ('open', 'in_progress')"),
            sqlite_where=text("state IN ('open', 'in_progress')"),
        ),
    )


class ReviewTaskRepository(ScopedRepository[ReviewTask]):
    model = ReviewTask

    async def get_active_for_document(self, document_id: uuid.UUID) -> ReviewTask | None:
        stmt = self._scoped_select().where(
            ReviewTask.document_id == document_id,
            ReviewTask.state.in_(_ACTIVE_STATES),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[ReviewTask]:
        stmt = (
            self._scoped_select()
            .where(ReviewTask.state.in_(_ACTIVE_STATES))
            .order_by(ReviewTask.priority, ReviewTask.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())


def _validate_reasons(reasons: Sequence[Any]) -> list[dict[str, Any]]:
    """Reasons must be non-empty and each linked to its source — a task
    that cannot say why it exists is not created."""
    if not reasons:
        raise ValueError("a review task needs at least one reason")
    validated: list[dict[str, Any]] = []
    for index, reason in enumerate(reasons):
        if not isinstance(reason, dict) or not reason.get("code") or not reason.get("message"):
            raise ValueError(f"reasons[{index}] needs 'code' and 'message'")
        if reason.get("field_key") is None and reason.get("rule_key") is None:
            raise ValueError(
                f"reasons[{index}] must link to a field_key or rule_key — "
                "unattributed reasons are not reviewable"
            )
        validated.append(dict(reason))
    return validated


async def _transition(
    session: AsyncSession,
    context: OrganizationContext,
    task: ReviewTask,
    to_state: ReviewTaskState,
    *,
    actor_id: str,
    actor_type: ActorType = ActorType.USER,
    summary: dict[str, Any] | None = None,
) -> None:
    if to_state.value not in _ALLOWED.get(task.state, frozenset()):
        raise InvalidReviewTaskTransitionError(task.id, task.state, to_state.value)
    from_state = task.state
    task.state = to_state.value
    await session.flush()
    await record_audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="review_task.state_changed",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=context.organization_id,
        summary={"from": from_state, "to": to_state.value, **(summary or {})},
    )


async def route_document_to_review(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    reasons: Sequence[Any],
    priority: int = 100,
    blocking: bool = False,
    sla_due_at: datetime | None = None,
    actor_id: str = "system:pipeline",
) -> ReviewTask:
    """Create the run's review task, superseding any earlier active task
    (a fresh run's findings replace a stale run's) so the one-active-task
    invariant holds through reprocessing."""
    repo = ReviewTaskRepository(session, context)
    existing = await repo.get_active_for_document(document_id)
    if existing is not None:
        await _transition(
            session,
            context,
            existing,
            ReviewTaskState.CANCELLED,
            actor_id=actor_id,
            actor_type=ActorType.SYSTEM,
            summary={"reason": "superseded by a newer processing run"},
        )
        existing.outcome = "superseded"
    task = repo.add(
        ReviewTask(
            document_id=document_id,
            run_id=run_id,
            reasons=_validate_reasons(reasons),
            priority=priority,
            blocking=blocking,
            sla_due_at=sla_due_at,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="review_task.created",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=context.organization_id,
        summary={
            "document_id": str(document_id),
            "run_id": str(run_id),
            "reasons": len(task.reasons),
            "priority": priority,
        },
    )
    return task


async def claim_task(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    user_id: str,
) -> ReviewTask:
    await _transition(
        session,
        context,
        task,
        ReviewTaskState.IN_PROGRESS,
        actor_id=user_id,
        summary={"assigned_to": user_id},
    )
    task.assigned_to = user_id
    task.assigned_at = utcnow()
    await session.flush()
    return task


async def release_task(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    actor_id: str,
) -> ReviewTask:
    await _transition(
        session,
        context,
        task,
        ReviewTaskState.OPEN,
        actor_id=actor_id,
        summary={"released_from": task.assigned_to},
    )
    task.assigned_to = None
    task.assigned_at = None
    await session.flush()
    return task


async def complete_task(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    outcome: str,
    actor_id: str,
) -> ReviewTask:
    if outcome not in ("approved", "rejected"):
        raise ValueError("review outcomes are 'approved' or 'rejected'")
    await _transition(
        session,
        context,
        task,
        ReviewTaskState.COMPLETED,
        actor_id=actor_id,
        summary={"outcome": outcome},
    )
    task.outcome = outcome
    task.completed_at = utcnow()
    task.completed_by = actor_id
    await session.flush()
    return task


async def cancel_task(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    reason: str,
    actor_id: str,
    actor_type: ActorType = ActorType.SYSTEM,
) -> ReviewTask:
    await _transition(
        session,
        context,
        task,
        ReviewTaskState.CANCELLED,
        actor_id=actor_id,
        actor_type=actor_type,
        summary={"reason": reason},
    )
    task.outcome = reason[:200]
    await session.flush()
    return task


async def escalate_task(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    reason: str,
    actor_id: str,
) -> ReviewTask:
    """Escalate: record why and who, raise the queue priority, and hand
    the task back to OPEN so a supervisor can claim ownership."""
    if not reason.strip():
        raise ValueError("escalation needs a reason")
    if task.state == ReviewTaskState.IN_PROGRESS.value:
        await _transition(
            session,
            context,
            task,
            ReviewTaskState.OPEN,
            actor_id=actor_id,
            summary={"escalated": True, "released_from": task.assigned_to},
        )
        task.assigned_to = None
        task.assigned_at = None
    elif task.state != ReviewTaskState.OPEN.value:
        raise InvalidReviewTaskTransitionError(task.id, task.state, "escalated")
    task.escalated_at = utcnow()
    task.escalated_by = actor_id
    task.escalation_reason = reason.strip()
    task.priority = min(task.priority, 10)  # escalations jump the queue
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="review_task.escalated",
        target_type="review_task",
        target_id=str(task.id),
        organization_id=context.organization_id,
        summary={"reason": reason.strip(), "priority": task.priority},
    )
    return task


async def cancel_active_task_for_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    reason: str,
    actor_id: str,
) -> ReviewTask | None:
    """Cancel + return the document's active task if one exists — used
    when the document itself is cancelled or sent back for reprocessing."""
    task = await ReviewTaskRepository(session, context).get_active_for_document(document_id)
    if task is None:
        return None
    return await cancel_task(session, context, task=task, reason=reason, actor_id=actor_id)


__all__ = [
    "InvalidReviewTaskTransitionError",
    "ReviewTask",
    "ReviewTaskRepository",
    "ReviewTaskState",
    "cancel_active_task_for_document",
    "cancel_task",
    "claim_task",
    "complete_task",
    "escalate_task",
    "release_task",
    "route_document_to_review",
]
