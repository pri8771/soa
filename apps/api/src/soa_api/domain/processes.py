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

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.domain.versioning import (
    ImmutablePublishedVersionMixin,
    ImmutableVersionError,
    InvalidVersionStateError,
    VersionState,
)
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.duplicate_po import (
    DuplicatePoPolicyValidationError,
    validate_po_duplicate_policy,
)
from soa_db.duplicate_policy import (
    DuplicatePolicyValidationError,
    validate_duplicate_policy,
)
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class ProcessStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProcessDefinitionError(ValueError):
    """A process default is invalid and cannot enter the version lifecycle."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


def validate_process_definition(definition: dict[str, Any]) -> None:
    """Validate process-level defaults shared by every child stream."""

    try:
        validate_duplicate_policy(definition)
    except DuplicatePolicyValidationError as exc:
        raise ProcessDefinitionError("duplicate_policy", str(exc)) from None
    try:
        validate_po_duplicate_policy(definition)
    except DuplicatePoPolicyValidationError as exc:
        raise ProcessDefinitionError("business_duplicate_policy", str(exc)) from None


__all__ = [
    "ImmutableVersionError",
    "InvalidVersionStateError",
    "Process",
    "ProcessDefinitionError",
    "ProcessRepository",
    "ProcessStatus",
    "ProcessVersion",
    "ProcessVersionRepository",
    "VersionState",
    "create_draft",
    "create_process",
    "publish_draft",
    "set_active_version",
    "validate_process_definition",
]


class Process(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "processes"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ProcessStatus.ACTIVE)
    # Points only at PUBLISHED versions; every move is audited.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)


class ProcessVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "process_versions"

    process_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    # Draft configuration content; richer typed artifacts (schema, rules,
    # policies) hang off this via CFG-003..005.
    definition: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("process_id", "version_number"),
        # At most ONE published version can exist at a time — the
        # database backstops the supersede logic against concurrent
        # first publishes (no prior row for optimistic locking to trip).
        Index(
            "uq_process_versions_single_published",
            "process_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


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
    effective_definition = dict(definition or {})
    validate_process_definition(effective_definition)
    versions = await ProcessVersionRepository(session, context).list_for_process(process.id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = ProcessVersionRepository(session, context).add(
        ProcessVersion(
            process_id=process.id,
            version_number=next_number,
            definition=effective_definition,
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
    # Revalidate at the sealing boundary. This catches legacy or directly
    # imported drafts that predate strict validation without altering how old
    # published snapshots are read at runtime.
    validate_process_definition(draft.definition)
    current = now or utcnow()
    repo = ProcessVersionRepository(session, context)
    previous = await repo.get_published(process.id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        # Flush the supersede before publishing: the single-published
        # unique index must never see two published rows mid-flush.
        await session.flush()
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
