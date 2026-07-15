"""Stream and stream-version models (CFG-002).

A Stream is one intake of documents into a Process ("Northstar — email
orders"). Stream configuration = the process version's defaults plus the
stream's EXPLICIT overrides. Publishing computes a resolved snapshot that
pins the process version and materializes every effective value, so the
processing pipeline never reads a mutable draft: everything it needs is
frozen inside the published StreamVersion. (The full provenance-tracking
resolver with fingerprints is CFG-006; this merge is its storage contract.)
"""

import hashlib
import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.domain.processes import ProcessVersion
from soa_api.domain.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class StreamStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Stream(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "streams"

    process_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=StreamStatus.ACTIVE)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class StreamVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "stream_versions"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    # ONLY the explicitly overridden values — inherited values are absent,
    # which is what lets the inheritance editor (CFG-013) show provenance.
    overrides: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    # Frozen at publish: pinned process version + every effective value.
    resolved_snapshot: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    pinned_process_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("stream_id", "version_number"),
        # At most ONE published version can exist at a time — the
        # database backstops the supersede logic against concurrent
        # first publishes (no prior row for optimistic locking to trip).
        Index(
            "uq_stream_versions_single_published",
            "stream_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


class StreamRepository(ScopedRepository[Stream]):
    model = Stream

    async def get_by_slug(self, slug: str) -> Stream | None:
        stmt = self._scoped_select().where(Stream.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class StreamVersionRepository(ScopedRepository[StreamVersion]):
    model = StreamVersion

    async def list_for_stream(self, stream_id: uuid.UUID) -> list[StreamVersion]:
        stmt = (
            self._scoped_select()
            .where(StreamVersion.stream_id == stream_id)
            .order_by(StreamVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, stream_id: uuid.UUID) -> StreamVersion | None:
        stmt = self._scoped_select().where(
            StreamVersion.stream_id == stream_id,
            StreamVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


def resolve_snapshot(process_version: ProcessVersion, overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge process defaults with explicit stream overrides (top-level keys;
    the provenance-aware deep resolver is CFG-006)."""
    snapshot = {
        "process_version_id": str(process_version.id),
        "process_version_number": process_version.version_number,
        "config": {**process_version.definition, **overrides},
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    snapshot["fingerprint"] = hashlib.sha256(encoded.encode()).hexdigest()
    return snapshot


async def create_stream(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process_id: uuid.UUID,
    name: str,
    slug: str,
    actor_id: str,
) -> Stream:
    stream = StreamRepository(session, context).add(
        Stream(process_id=process_id, name=name, slug=slug)
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="stream.created",
        target_type="stream",
        target_id=str(stream.id),
        organization_id=context.organization_id,
        summary={"slug": slug, "process_id": str(process_id)},
    )
    return stream


async def create_stream_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream: Stream,
    overrides: dict[str, Any] | None = None,
    change_summary: str | None = None,
    actor_id: str,
) -> StreamVersion:
    versions = await StreamVersionRepository(session, context).list_for_stream(stream.id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = StreamVersionRepository(session, context).add(
        StreamVersion(
            stream_id=stream.id,
            version_number=next_number,
            overrides=dict(overrides or {}),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="stream.draft_created",
        target_type="stream_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={"stream_id": str(stream.id), "version_number": next_number},
    )
    return draft


async def publish_stream_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream: Stream,
    draft: StreamVersion,
    process_version: ProcessVersion,
    actor_id: str,
    now: datetime | None = None,
) -> StreamVersion:
    """Publish a stream draft against a PUBLISHED (or superseded — i.e.
    immutable) process version. The snapshot freezes everything the
    pipeline needs; later process drafts cannot leak into it."""
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    if draft.stream_id != stream.id:
        raise InvalidVersionStateError("draft belongs to a different stream")
    if process_version.state == VersionState.DRAFT:
        raise InvalidVersionStateError(
            "streams pin immutable process versions — publish the process draft first"
        )
    if process_version.process_id != stream.process_id:
        raise InvalidVersionStateError("process version belongs to a different process")
    current = now or utcnow()
    previous = await StreamVersionRepository(session, context).get_published(stream.id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        # Flush the supersede before publishing: the single-published
        # unique index must never see two published rows mid-flush.
        await session.flush()
    draft.resolved_snapshot = resolve_snapshot(process_version, draft.overrides)
    draft.pinned_process_version_id = process_version.id
    draft.state = VersionState.PUBLISHED
    draft.published_at = current
    draft.published_by = actor_id
    stream.active_version_id = draft.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="stream.version_published",
        target_type="stream_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "stream_id": str(stream.id),
            "version_number": draft.version_number,
            "pinned_process_version_id": str(process_version.id),
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft


async def set_active_stream_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream: Stream,
    version: StreamVersion,
    actor_id: str,
    reason: str | None = None,
) -> Stream:
    """Move the stream's active pointer (rollback/roll-forward), mirroring
    the process helper: published or superseded targets only, audited."""
    if version.stream_id != stream.id:
        raise InvalidVersionStateError("version belongs to a different stream")
    if version.state == VersionState.DRAFT:
        raise InvalidVersionStateError("the active pointer can never reference a draft")
    previous_id = stream.active_version_id
    stream.active_version_id = version.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="stream.active_version_changed",
        target_type="stream",
        target_id=str(stream.id),
        organization_id=context.organization_id,
        summary={
            "from_version_id": str(previous_id) if previous_id else None,
            "to_version_id": str(version.id),
            "to_version_number": version.version_number,
            "reason": reason,
        },
    )
    return stream
