"""Per-tenant outbox delivery destination (EXP-012).

Outbox events (the transactional domain-event stream) publish to one
GLOBAL URL for every organization by default (``SOA_WORKER_OUTBOX_PUBLISH_URL``).
A real multi-tenant deployment needs each org to receive its own events at
its own receiver. This table lets an org admin configure that per-org
override; the worker falls back to the global URL when no active row
exists (``apps/worker/src/soa_worker/main.py``'s ``publish_outbox`` handler).

**No signing-secret column.** The global ``outbox_signing_secret`` continues
to sign every delivery regardless of destination in this iteration — a
per-org secret is a follow-up, not implemented here.
"""

import uuid

from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionConflictError, VersionedMixin
from soa_db.repository import OrganizationScopedMixin


class OutboxDestination(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "outbox_destinations"

    destination_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    __table_args__ = (UniqueConstraint("organization_id"),)


async def get_destination(
    session: AsyncSession, organization_id: uuid.UUID
) -> OutboxDestination | None:
    """The org's configured destination, if any (active or not — callers
    that only want live routing check ``is_active`` themselves)."""
    stmt = select(OutboxDestination).where(OutboxDestination.organization_id == organization_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_destination(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    destination_url: str,
    is_active: bool = True,
    if_match: int | None = None,
    actor_id: str,
) -> OutboxDestination:
    """Create or update the org's one destination row.

    ``if_match`` enforces optimistic concurrency on an existing row (API
    If-Match precondition, 409 on mismatch via ``VersionConflictError``);
    it is ignored when no row exists yet (there is nothing to conflict with).
    """
    existing = await get_destination(session, organization_id)
    if existing is None:
        destination = OutboxDestination(
            organization_id=organization_id,
            destination_url=destination_url,
            is_active=is_active,
        )
        session.add(destination)
        await session.flush()
        await record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            action="outbox_destination.created",
            target_type="outbox_destination",
            target_id=str(destination.id),
            organization_id=organization_id,
            summary={"is_active": is_active},
        )
        return destination
    if if_match is not None:
        existing.expect_version(if_match)
    existing.destination_url = destination_url
    existing.is_active = is_active
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="outbox_destination.updated",
        target_type="outbox_destination",
        target_id=str(existing.id),
        organization_id=organization_id,
        summary={"is_active": is_active},
    )
    return existing


async def delete_destination(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    if_match: int | None = None,
    actor_id: str,
) -> bool:
    """Remove the org's destination row, reverting it to the global
    fallback. Returns False when there was nothing to delete."""
    existing = await get_destination(session, organization_id)
    if existing is None:
        return False
    if if_match is not None:
        existing.expect_version(if_match)
    destination_id = existing.id
    await session.delete(existing)
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="outbox_destination.deleted",
        target_type="outbox_destination",
        target_id=str(destination_id),
        organization_id=organization_id,
        summary={},
    )
    return True


__all__ = [
    "OutboxDestination",
    "VersionConflictError",
    "delete_destination",
    "get_destination",
    "upsert_destination",
]
