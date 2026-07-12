"""Process and process-version models (CFG-001, ARCHITECTURE §configuration).

A Process is the stable tenant-owned root ("Purchase orders — Northstar").
Its configuration lives in ProcessVersions: drafts are editable under
optimistic concurrency; PUBLISHED versions are immutable forever — a
SQLAlchemy flush guard rejects any mutation of a published or superseded
version, so processed documents can always point at the exact
configuration that processed them. The process's active-version pointer
only ever moves to a published version, and every move is audited.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, UniqueConstraint, event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class ProcessStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class VersionState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class ImmutableVersionError(Exception):
    def __init__(self, version_id: uuid.UUID, state: str) -> None:
        super().__init__(
            f"process version {version_id} is {state} and immutable — "
            "create a new draft instead of editing history"
        )


class InvalidVersionStateError(Exception):
    pass


class Process(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "processes"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ProcessStatus.ACTIVE)
    # Points only at PUBLISHED versions; every move is audited.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class ProcessVersion(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "process_versions"

    process_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=VersionState.DRAFT)
    # Draft configuration content; richer typed artifacts (schema, rules,
    # policies) hang off this via CFG-003..005.
    definition: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (UniqueConstraint("process_id", "version_number"),)


@event.listens_for(Session, "before_flush")
def _reject_published_mutations(session: Session, flush_context: object, instances: object) -> None:
    for entity in session.dirty:
        if not isinstance(entity, ProcessVersion):
            continue
        if not session.is_modified(entity, include_collections=False):
            continue
        insp = inspect(entity)
        state_hist = insp.attrs.state.history
        previous_state = state_hist.deleted[0] if state_hist.deleted else entity.state
        if previous_state in (VersionState.PUBLISHED, VersionState.SUPERSEDED):
            # The only legal touch of a published version is superseding it.
            only_state_changed = all(
                attr.key == "state" or not attr.history.has_changes()
                for attr in insp.attrs
                if attr.key not in ("updated_at", "version")
            )
            is_supersede = (
                previous_state == VersionState.PUBLISHED
                and entity.state == VersionState.SUPERSEDED
                and only_state_changed
            )
            if not is_supersede:
                raise ImmutableVersionError(entity.id, str(previous_state))
    for entity in session.deleted:
        if isinstance(entity, ProcessVersion) and entity.state != VersionState.DRAFT:
            raise ImmutableVersionError(entity.id, str(entity.state))


class ProcessRepository(ScopedRepository[Process]):
    model = Process

    async def get_by_slug(self, slug: str) -> Process | None:
        stmt = self._scoped_select().where(Process.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class ProcessVersionRepository(ScopedRepository[ProcessVersion]):
    model = ProcessVersion

    async def list_for_process(self, process_id: uuid.UUID) -> list[ProcessVersion]:
        stmt = (
            self._scoped_select()
            .where(ProcessVersion.process_id == process_id)
            .order_by(ProcessVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, process_id: uuid.UUID) -> ProcessVersion | None:
        stmt = self._scoped_select().where(
            ProcessVersion.process_id == process_id,
            ProcessVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Lifecycle helpers — every consequential mutation is audited. The full
# validate/test/publish pipeline arrives with CFG-007; these enforce the
# CFG-001 invariants.
# ---------------------------------------------------------------------------


async def create_process(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    name: str,
    slug: str,
    actor_id: str,
) -> Process:
    process = ProcessRepository(session, context).add(Process(name=name, slug=slug))
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="process.created",
        target_type="process",
        target_id=str(process.id),
        organization_id=context.organization_id,
        summary={"slug": slug, "name": name},
    )
    return process


async def create_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process: Process,
    definition: dict[str, Any] | None = None,
    change_summary: str | None = None,
    actor_id: str,
) -> ProcessVersion:
    """Add the next draft version. Content may seed from any prior version;
    the version number always increases monotonically."""
    versions = await ProcessVersionRepository(session, context).list_for_process(process.id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = ProcessVersionRepository(session, context).add(
        ProcessVersion(
            process_id=process.id,
            version_number=next_number,
            definition=dict(definition or {}),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="process.draft_created",
        target_type="process_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={"process_id": str(process.id), "version_number": next_number},
    )
    return draft


async def publish_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process: Process,
    draft: ProcessVersion,
    actor_id: str,
    now: datetime | None = None,
) -> ProcessVersion:
    """Publish a draft: the prior published version is superseded, the
    draft becomes the published one, and the active pointer moves — one
    transaction, fully audited."""
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    if draft.process_id != process.id:
        raise InvalidVersionStateError("draft belongs to a different process")
    current = now or utcnow()
    repo = ProcessVersionRepository(session, context)
    previous = await repo.get_published(process.id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
    draft.state = VersionState.PUBLISHED
    draft.published_at = current
    draft.published_by = actor_id
    process.active_version_id = draft.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="process.version_published",
        target_type="process_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "process_id": str(process.id),
            "version_number": draft.version_number,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft


async def set_active_version(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process: Process,
    version: ProcessVersion,
    actor_id: str,
    reason: str | None = None,
) -> Process:
    """Move the active pointer (rollback/roll-forward). Only published or
    superseded versions are eligible — never drafts."""
    if version.process_id != process.id:
        raise InvalidVersionStateError("version belongs to a different process")
    if version.state == VersionState.DRAFT:
        raise InvalidVersionStateError("the active pointer can never reference a draft")
    previous_id = process.active_version_id
    process.active_version_id = version.id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="process.active_version_changed",
        target_type="process",
        target_id=str(process.id),
        organization_id=context.organization_id,
        summary={
            "from_version_id": str(previous_id) if previous_id else None,
            "to_version_id": str(version.id),
            "to_version_number": version.version_number,
            "reason": reason,
        },
    )
    return process
