"""Approval and rejection service (REV-012).

Approval is the moment a document's data becomes the organization's
data, so it is deliberately strict:

- FINAL validation happens here, at the moment of approval — the rules
  re-run over the effective (corrected) values, never a stale verdict
  from minutes ago.
- CRITICAL blockers cannot be bypassed casually: approving past them
  requires the separate ``documents.approve.override`` grant AND a
  written override reason, both captured in the audit trail.
- Remaining non-blocking warnings never block, but they are recorded on
  the approval so downstream consumers know what was accepted as-is.
- A second-approval hook lets policy demand two distinct approvers: the
  first approval is recorded and the task returns to OPEN for a
  different reviewer to finish. The default policy requires none —
  honestly, because no tenant policy configures it yet (CFG-005 policy
  roots will).
- The whole operation is one transaction, and duplicates are idempotent:
  re-approving an approved task succeeds without side effects (the
  outbox dedupe key additionally guarantees the export trigger fires at
  most once per run even across replays).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.services.revalidation import revalidate_run
from soa_db.audit import ActorType, record_audit_event
from soa_db.documents import Document, DocumentState, transition_document
from soa_db.outbox import enqueue_event
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import ReviewTask, ReviewTaskState, complete_task, release_task
from soa_db.types import utcnow


class ApprovalStateError(Exception):
    """The task/document is not in a state where this action applies (409)."""


class ApprovalPermissionError(Exception):
    """The actor is not allowed to perform this action (403)."""


class CriticalBlockersError(Exception):
    """Critical blockers remain and no override was requested (409)."""

    def __init__(self, rule_keys: list[str]) -> None:
        self.rule_keys = rule_keys
        listed = ", ".join(rule_keys) or "unresolved critical findings"
        super().__init__(
            f"Critical blockers remain ({listed}); resolve them or approve "
            "with an authorized override reason."
        )


@dataclass(frozen=True)
class ApprovalReview:
    """What a second-approval policy gets to look at."""

    task: ReviewTask
    evaluation: dict[str, Any]
    decision: dict[str, Any]
    override_used: bool


SecondApprovalHook = Callable[[ApprovalReview], bool]


def no_second_approval(review: ApprovalReview) -> bool:
    """Default policy: dual approval is not configured for any tenant yet.
    The enforcement path exists and is tested; CFG policy roots will make
    this configurable per stream."""
    return False


def _require_active_assignee(task: ReviewTask, actor: str, action: str) -> None:
    if task.state != ReviewTaskState.IN_PROGRESS.value:
        raise ApprovalStateError(f"The task is {task.state}; claim it before {action}.")
    if task.assigned_to != actor:
        raise ApprovalPermissionError(
            f"The task is assigned to {task.assigned_to}; only the assignee may finish {action}."
        )


async def approve_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    document: Document,
    actor: str,
    can_override: bool,
    override_reason: str | None = None,
    second_approval: SecondApprovalHook = no_second_approval,
) -> dict[str, Any]:
    # Idempotent duplicate: an approved task stays approved, silently.
    if task.state == ReviewTaskState.COMPLETED.value:
        if task.outcome == "approved":
            return {
                "status": "approved",
                "idempotent": True,
                "task_version": task.version,
                "warnings": [],
                "override_used": False,
            }
        raise ApprovalStateError(f"The task was already {task.outcome}; it cannot be approved.")
    _require_active_assignee(task, actor, "approving")
    if document.state != DocumentState.REVIEW_REQUIRED.value:
        raise ApprovalStateError(
            f"The document is {document.state}; only documents in review can be approved."
        )

    # Final validation NOW, over the effective (corrected) values.
    revalidation = await revalidate_run(session, context, document=document, run_id=task.run_id)
    evaluation = revalidation["evaluation"]
    decision = revalidation["decision"]
    remaining: list[dict[str, Any]] = decision.get("reasons", [])

    override_used = False
    if evaluation.get("blocking"):
        blocker_keys = sorted(
            {r["rule_key"] for r in remaining if r.get("rule_key")}
            | {r["field_key"] for r in remaining if r.get("field_key") and not r.get("rule_key")}
        )
        if override_reason is None or not override_reason.strip():
            raise CriticalBlockersError(blocker_keys)
        if not can_override:
            raise ApprovalPermissionError(
                "Overriding critical blockers requires the documents.approve.override permission."
            )
        override_used = True

    review = ApprovalReview(
        task=task, evaluation=evaluation, decision=decision, override_used=override_used
    )
    if second_approval(review):
        if task.first_approved_by is None:
            task.first_approved_by = actor
            task.first_approved_at = utcnow()
            await release_task(session, context, task=task, actor_id=actor)
            await record_audit_event(
                session,
                actor_type=ActorType.USER,
                actor_id=actor,
                action="review_task.first_approval_recorded",
                target_type="review_task",
                target_id=str(task.id),
                organization_id=context.organization_id,
                summary={"override_used": override_used},
            )
            return {
                "status": "pending_second_approval",
                "idempotent": False,
                "task_version": task.version,
                "warnings": remaining,
                "override_used": override_used,
            }
        if task.first_approved_by == actor:
            raise ApprovalPermissionError(
                "This approval requires a SECOND approver; you already recorded the first approval."
            )

    await complete_task(session, context, task=task, outcome="approved", actor_id=actor)
    await transition_document(
        session,
        context,
        document=document,
        to_state=DocumentState.APPROVED,
        reason="approved in review" + (" (override)" if override_used else ""),
        actor_type=ActorType.USER,
        actor_id=actor,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor,
        action="document.approved",
        target_type="document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        summary={
            "task_id": str(task.id),
            "run_id": str(task.run_id),
            "remaining_warnings": [
                {
                    "code": r.get("code"),
                    "rule_key": r.get("rule_key"),
                    "field_key": r.get("field_key"),
                }
                for r in remaining
            ],
            "override_used": override_used,
            **({"override_reason": override_reason} if override_used else {}),
            **(
                {"first_approved_by": task.first_approved_by}
                if task.first_approved_by and task.first_approved_by != actor
                else {}
            ),
        },
    )
    # The export trigger seam (EXP-008): at most once per run, even if a
    # replayed request slips past the state checks.
    await enqueue_event(
        session,
        event_type="document.approved",
        payload={
            "document_id": str(document.id),
            "run_id": str(task.run_id),
            "task_id": str(task.id),
            "approved_by": actor,
            "override_used": override_used,
        },
        organization_id=context.organization_id,
        dedupe_key=f"document.approved:{document.id}:{task.run_id}",
    )
    return {
        "status": "approved",
        "idempotent": False,
        "task_version": task.version,
        "warnings": remaining,
        "override_used": override_used,
    }


async def reject_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    task: ReviewTask,
    document: Document,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("rejection needs a reason")
    if task.state == ReviewTaskState.COMPLETED.value:
        if task.outcome == "rejected":
            return {"status": "rejected", "idempotent": True, "task_version": task.version}
        raise ApprovalStateError(f"The task was already {task.outcome}; it cannot be rejected.")
    _require_active_assignee(task, actor, "rejecting")
    if document.state != DocumentState.REVIEW_REQUIRED.value:
        raise ApprovalStateError(
            f"The document is {document.state}; only documents in review can be rejected."
        )

    await complete_task(session, context, task=task, outcome="rejected", actor_id=actor)
    await transition_document(
        session,
        context,
        document=document,
        to_state=DocumentState.REJECTED,
        reason=reason.strip()[:500],
        actor_type=ActorType.USER,
        actor_id=actor,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor,
        action="document.rejected",
        target_type="document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        summary={"task_id": str(task.id), "run_id": str(task.run_id), "reason": reason.strip()},
    )
    await enqueue_event(
        session,
        event_type="document.rejected",
        payload={
            "document_id": str(document.id),
            "run_id": str(task.run_id),
            "task_id": str(task.id),
            "rejected_by": actor,
        },
        organization_id=context.organization_id,
        dedupe_key=f"document.rejected:{document.id}:{task.run_id}",
    )
    return {"status": "rejected", "idempotent": False, "task_version": task.version}
