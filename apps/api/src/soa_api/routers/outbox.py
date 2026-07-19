"""Outbox dead-letter inspection and replay, and per-tenant destination
routing (EXP-012).

Outbox events publish to one GLOBAL URL for every organization by default
(``SOA_WORKER_OUTBOX_PUBLISH_URL``); the destination endpoints below let an
org admin route their own tenant's events to their own receiver instead.
**No signing-secret column** — the deployment's global
``outbox_signing_secret`` continues to sign every delivery regardless of
which destination it goes to; a per-org secret is a follow-up, not
implemented here.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, SettingsDep
from soa_db.audit import ActorType, record_audit_event
from soa_db.mixins import VersionConflictError
from soa_db.outbox import OutboxEvent, OutboxStatus, replay_failed_event
from soa_db.outbox_destinations import (
    OutboxDestination,
    delete_destination,
    get_destination,
    upsert_destination,
)
from soa_integrations import DestinationRefusedError, validate_destination

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
        previous_attempts = await replay_failed_event(session, event)
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
        summary={"event_type": event.event_type, "previous_attempts": previous_attempts},
    )
    return {"id": str(event.id), "status": event.status, "attempts": event.attempts}


def _serialize_destination(destination: OutboxDestination) -> dict[str, Any]:
    return {
        "id": str(destination.id),
        "destination_url": destination.destination_url,
        "is_active": destination.is_active,
        "version": destination.version,
        "created_at": destination.created_at.isoformat(),
        "updated_at": destination.updated_at.isoformat(),
    }


class OutboxDestinationRequest(BaseModel):
    destination_url: str = Field(min_length=1, max_length=2000)
    is_active: bool = True


@router.get("/orgs/{organization_slug}/outbox-destination")
async def get_outbox_destination(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
) -> dict[str, Any] | None:
    """Null when the org has no override — the worker falls back to the
    deployment-wide global URL in that case."""
    destination = await get_destination(session, authorized.org_context.organization_id)
    return _serialize_destination(destination) if destination is not None else None


@router.put("/orgs/{organization_slug}/outbox-destination")
async def put_outbox_destination(
    body: OutboxDestinationRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
    settings: SettingsDep,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Create or replace the org's one destination. Validated with the same
    HTTPS/exact-host/public-address/allowlist policy as every other
    outbound destination (``soa_integrations.validate_destination``) — the
    worker re-validates again at publish time in case the allowlist or DNS
    answer has since changed."""
    try:
        validate_destination(
            body.destination_url, allowlist=settings.outbound_destination_allowlist
        )
    except DestinationRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        destination = await upsert_destination(
            session,
            authorized.org_context.organization_id,
            destination_url=body.destination_url,
            is_active=body.is_active,
            if_match=if_match,
            actor_id=authorized.principal.subject,
        )
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _serialize_destination(destination)


@router.delete("/orgs/{organization_slug}/outbox-destination", status_code=204)
async def delete_outbox_destination(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> None:
    """Reverts the org to the deployment-wide global destination."""
    try:
        deleted = await delete_destination(
            session,
            authorized.org_context.organization_id,
            if_match=if_match,
            actor_id=authorized.principal.subject,
        )
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="No outbox destination configured.")
