"""HTTP publication for one durable transactional-outbox event."""

import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.outbox import OutboxEvent, OutboxStatus, mark_failed, mark_published


@dataclass(frozen=True)
class PublishResult:
    outcome: str  # published | retryable_error | dead_letter | no_op
    status_code: int | None = None


async def publish_outbox_event(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    destination_url: str,
    client: httpx.AsyncClient,
) -> PublishResult:
    event = (
        await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise ValueError("outbox event does not exist")
    if event.status == OutboxStatus.PUBLISHED:
        return PublishResult("no_op")
    if event.status == OutboxStatus.FAILED:
        return PublishResult("dead_letter")
    try:
        response = await client.post(
            destination_url,
            json={
                "id": str(event.id),
                "type": event.event_type,
                "organization_id": (str(event.organization_id) if event.organization_id else None),
                "correlation_id": event.correlation_id,
                "occurred_at": event.created_at.isoformat(),
                "payload": event.payload,
            },
            headers={"Idempotency-Key": str(event.id)},
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        mark_failed(event, error=f"publication failed ({type(error).__name__})")
        return PublishResult(
            "dead_letter" if event.status == OutboxStatus.FAILED else "retryable_error",
            getattr(getattr(error, "response", None), "status_code", None),
        )
    mark_published(event)
    return PublishResult("published", response.status_code)
