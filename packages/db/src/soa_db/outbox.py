"""Transactional outbox (ADR-008).

Domain state and follow-up events commit in ONE transaction: the event row
is added to the same session as the domain change, so a rollback discards
both and a commit publishes both. A separate publisher drains pending rows
and dispatches them; publishing is idempotent and duplicate enqueues are
absorbed by the dedupe key.
"""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Index, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.base import Base
from soa_db.mixins import UuidPrimaryKeyMixin
from soa_db.types import GUID, UTCDateTime, utcnow

PORTABLE_JSON = JSON().with_variant(JSONB(), "postgresql")


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class OutboxEvent(UuidPrimaryKeyMixin, Base):
    __tablename__ = "outbox_events"

    # Nullable: system-level events have no tenant; tenant events must set it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(300), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=OutboxStatus.PENDING)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (Index("ix_outbox_events_claim", "status", "next_attempt_at"),)


async def enqueue_event(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    organization_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    correlation_id: str | None = None,
) -> OutboxEvent:
    """Stage an event in the caller's transaction.

    With a ``dedupe_key``, a second enqueue returns the existing event
    instead of creating a duplicate. The unique constraint backs this up
    against concurrent writers — an IntegrityError there means another
    transaction already enqueued the same intent.
    """
    if dedupe_key is not None:
        existing = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    event = OutboxEvent(
        event_type=event_type,
        payload=payload,
        organization_id=organization_id,
        dedupe_key=dedupe_key,
        correlation_id=correlation_id,
    )
    session.add(event)
    await session.flush()
    # Each event gets an independently retryable durable publication job.
    # The local import avoids making the jobs schema depend on this module.
    from soa_db.jobs import enqueue_job

    await enqueue_job(
        session,
        job_type="outbox.publish",
        organization_id=organization_id,
        payload={"outbox_event_id": str(event.id)},
        dedupe_key=f"outbox:{event.id}",
        max_attempts=10,
    )
    return event


async def replay_failed_event(session: AsyncSession, event: OutboxEvent) -> int:
    """Reset one dead-lettered event and enqueue a fresh delivery attempt.

    Returns the exhausted attempt count so the API can preserve it in the
    replay audit record even though the live delivery state is reset.
    """
    if event.status != OutboxStatus.FAILED:
        raise ValueError("only failed outbox events can be replayed")
    previous_attempts = event.attempts
    previous_delivery_cursor = event.next_attempt_at.isoformat()
    replayed_at = utcnow()
    event.status = OutboxStatus.PENDING
    event.attempts = 0
    event.published_at = None
    event.next_attempt_at = replayed_at
    event.last_error = None
    from soa_db.jobs import enqueue_job

    await enqueue_job(
        session,
        job_type="outbox.publish",
        organization_id=event.organization_id,
        payload={"outbox_event_id": str(event.id)},
        # The prior delivery cursor is stable for concurrent requests but
        # changes after each replay, giving every exhausted generation one
        # idempotent replay job even though attempt counts reset and repeat.
        dedupe_key=(f"outbox:{event.id}:replay:{previous_attempts}:{previous_delivery_cursor}"),
        max_attempts=10,
    )
    return previous_attempts


async def claim_pending_events(
    session: AsyncSession,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> list[OutboxEvent]:
    """Fetch due pending events for publication (oldest first).

    On PostgreSQL the publisher wraps this in FOR UPDATE SKIP LOCKED via the
    job layer (JOB-003); the query itself stays portable.
    """
    current = now or utcnow()
    stmt = (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxStatus.PENDING,
            OutboxEvent.next_attempt_at <= current,
        )
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


def mark_published(event: OutboxEvent, *, now: datetime | None = None) -> None:
    """Idempotent: publishing an already-published event is a no-op."""
    if event.status == OutboxStatus.PUBLISHED:
        return
    event.status = OutboxStatus.PUBLISHED
    event.published_at = now or utcnow()


def mark_failed(
    event: OutboxEvent,
    *,
    error: str,
    retry_in: timedelta | None = None,
    max_attempts: int = 10,
    terminal: bool = False,
    now: datetime | None = None,
) -> None:
    """Record a failed publication attempt with bounded retries."""
    current = now or utcnow()
    event.attempts += 1
    event.last_error = error[:500]
    if terminal or event.attempts >= max_attempts:
        event.status = OutboxStatus.FAILED
        return
    delay = retry_in if retry_in is not None else timedelta(seconds=min(2**event.attempts, 300))
    event.next_attempt_at = current + delay
