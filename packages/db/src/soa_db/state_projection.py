"""Document current-state projection (PRC-002).

The document row's ``state`` column is a PROJECTION of the append-only
audit trail: ``document.received`` starts every history and each
``document.state_changed`` event advances it. This module replays that
trail deterministically, verifying continuity (each event's ``from``
must equal the state the replay arrived at), and compares the result
against the stored column — any disagreement is drift worth alarming
on, because it means a write path skipped the transition service.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.audit import AuditEvent
from soa_db.documents import Document, DocumentState
from soa_db.repository import OrganizationContext


class ProjectionError(Exception):
    """The audit trail is not a coherent state history."""


@dataclass(frozen=True)
class TransitionRecord:
    from_state: str
    to_state: str
    actor_type: str
    actor_id: str
    reason: str | None
    occurred_at: str
    correlation_id: str | None


@dataclass(frozen=True)
class ProjectedState:
    state: str
    history: list[TransitionRecord]


async def project_document_state(
    session: AsyncSession, context: OrganizationContext, document_id: uuid.UUID
) -> ProjectedState:
    """Replay the document's audit trail into its current state."""
    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.organization_id == context.organization_id,
                    AuditEvent.target_type == "document",
                    AuditEvent.target_id == str(document_id),
                    AuditEvent.action.in_(["document.received", "document.state_changed"]),
                )
                .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not events or events[0].action != "document.received":
        raise ProjectionError(f"document {document_id}: history must begin with document.received")

    state = DocumentState.RECEIVED.value
    history: list[TransitionRecord] = []
    for event in events[1:]:
        if event.action != "document.state_changed":
            continue
        summary = event.summary or {}
        from_state = str(summary.get("from"))
        to_state = str(summary.get("to"))
        if from_state != state:
            raise ProjectionError(
                f"document {document_id}: event claims transition from {from_state!r} "
                f"but the replayed state is {state!r} — the trail has a gap"
            )
        history.append(
            TransitionRecord(
                from_state=from_state,
                to_state=to_state,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                reason=(str(summary["reason"]) if summary.get("reason") else None),
                occurred_at=event.occurred_at.isoformat(),
                correlation_id=event.correlation_id,
            )
        )
        state = to_state
    return ProjectedState(state=state, history=history)


async def verify_state_projection(
    session: AsyncSession, context: OrganizationContext, document: Document
) -> ProjectedState:
    """Replay and compare against the stored column; raise on drift."""
    projected = await project_document_state(session, context, document.id)
    if projected.state != document.state:
        raise ProjectionError(
            f"document {document.id}: stored state {document.state!r} disagrees with "
            f"the audit trail projection {projected.state!r}"
        )
    return projected
