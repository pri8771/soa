"""Outbox dead-letter inspection and replay."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import OutboxEvent, OutboxStatus, replay_failed_event

router = APIRouter(tags=["outbox"])


@router.get("/orgs/{organization_slug}/outbox/dead-letter")
async def list_dead_letters(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.read"))],
    session: DbSession,
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.organization_id == authorized.org_context.organization_id,
                    OutboxEvent.status == OutboxStatus.FAILED,
                )
                .order_by(OutboxEvent.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "event_type": row.event_type,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.post("/orgs/{organization_slug}/outbox/{event_id}/replay")
async def replay_dead_letter(
    event_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    event = (
        await session.execute(
            select(OutboxEvent).where(
                OutboxEvent.id == event_id,
                OutboxEvent.organization_id == authorized.org_context.organization_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Outbox event not found.")
    try:
        await replay_failed_event(session, event)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=authorized.principal.subject,
        action="outbox.replayed",
        target_type="outbox_event",
        target_id=str(event.id),
        organization_id=authorized.org_context.organization_id,
        summary={"event_type": event.event_type, "previous_attempts": event.attempts},
    )
    return {"id": str(event.id), "status": event.status}
