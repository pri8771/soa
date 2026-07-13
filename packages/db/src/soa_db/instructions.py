"""Versioned extraction instructions (AIO-010).

Instructions are the prompt content model-based extraction adapters
carry: a system preamble plus optional per-field guidance, versioned
per (organization, stream version) with the CFG discipline —
draft → published → superseded, published rows immutable (the
versioning flush guard refuses edits). Every extraction call references
the EXACT version via :attr:`InstructionVersion.reference`, which is
what evidence records store as ``prompt_or_instruction_version``.

The linked stream/schema versions are UUID references without foreign
keys: those tables belong to the API's configuration context, and the
API service layer validates existence before a draft is created —
the worker only ever reads published instruction rows.

Content is SENSITIVE configuration (it can encode business logic and
field semantics): API access is gated by the dedicated
``instructions.read`` / ``instructions.manage`` permissions, and audit
events record THAT content changed plus its size — never the text.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow
from soa_db.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)

#: Bounds: instructions travel inside every model request.
MAX_INSTRUCTIONS_CHARS = 20_000
MAX_FIELD_GUIDANCE_ENTRIES = 200


class InstructionValidationError(ValueError):
    pass


def validate_instruction_content(content: dict[str, Any]) -> None:
    instructions = content.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise InstructionValidationError("content needs a non-empty 'instructions' string")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise InstructionValidationError(
            f"instructions exceed {MAX_INSTRUCTIONS_CHARS} characters — they travel "
            "inside every model request and must stay bounded"
        )
    guidance = content.get("field_guidance", {})
    if not isinstance(guidance, dict):
        raise InstructionValidationError("field_guidance must map field keys to guidance strings")
    if len(guidance) > MAX_FIELD_GUIDANCE_ENTRIES:
        raise InstructionValidationError(
            f"field_guidance exceeds {MAX_FIELD_GUIDANCE_ENTRIES} entries"
        )
    for key, value in guidance.items():
        if not isinstance(value, str) or not value.strip():
            raise InstructionValidationError(f"field_guidance[{key!r}] must be a non-empty string")
    unknown = set(content) - {"instructions", "field_guidance"}
    if unknown:
        raise InstructionValidationError(
            f"unknown content keys: {sorted(unknown)} — the shape is closed"
        )


class InstructionVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "instruction_versions"

    #: Cross-context references (no FK; the API validates existence).
    stream_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    schema_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "stream_version_id", "version_number"),
        Index(
            "uq_instruction_versions_single_published",
            "organization_id",
            "stream_version_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )

    @property
    def reference(self) -> str:
        """The exact-version string every extraction call carries and
        evidence records store."""
        return f"instruction:{self.id}:v{self.version_number}"


class InstructionVersionRepository(ScopedRepository[InstructionVersion]):
    model = InstructionVersion

    async def list_for_stream_version(
        self, stream_version_id: uuid.UUID
    ) -> list[InstructionVersion]:
        stmt = (
            self._scoped_select()
            .where(InstructionVersion.stream_version_id == stream_version_id)
            .order_by(InstructionVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, stream_version_id: uuid.UUID) -> InstructionVersion | None:
        stmt = self._scoped_select().where(
            InstructionVersion.stream_version_id == stream_version_id,
            InstructionVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


def _content_summary(content: dict[str, Any]) -> dict[str, Any]:
    """Audit facts about the content — never the content itself."""
    return {
        "instructions_chars": len(content.get("instructions", "")),
        "field_guidance_entries": len(content.get("field_guidance", {}) or {}),
    }


async def create_instruction_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_version_id: uuid.UUID,
    schema_version_id: uuid.UUID,
    content: dict[str, Any],
    change_summary: str | None = None,
    actor_id: str,
) -> InstructionVersion:
    validate_instruction_content(content)
    repo = InstructionVersionRepository(session, context)
    versions = await repo.list_for_stream_version(stream_version_id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = repo.add(
        InstructionVersion(
            stream_version_id=stream_version_id,
            schema_version_id=schema_version_id,
            version_number=next_number,
            content=dict(content),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="instructions.draft_created",
        target_type="instruction_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "stream_version_id": str(stream_version_id),
            "version_number": next_number,
            **_content_summary(content),
        },
    )
    return draft


async def update_instruction_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: InstructionVersion,
    content: dict[str, Any],
    change_summary: str | None = None,
    actor_id: str,
) -> InstructionVersion:
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts are editable; this version is {draft.state!r}")
    validate_instruction_content(content)
    draft.content = dict(content)
    if change_summary is not None:
        draft.change_summary = change_summary
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="instructions.draft_updated",
        target_type="instruction_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary=_content_summary(content),
    )
    return draft


async def publish_instruction_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: InstructionVersion,
    actor_id: str,
    now: datetime | None = None,
) -> InstructionVersion:
    """Publish a draft: the previously published version for the same
    stream version is superseded (kept, immutable) and the published
    content freezes forever."""
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    validate_instruction_content(draft.content)
    previous = await InstructionVersionRepository(session, context).get_published(
        draft.stream_version_id
    )
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        await session.flush()
    draft.state = VersionState.PUBLISHED
    draft.published_at = now or utcnow()
    draft.published_by = actor_id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="instructions.published",
        target_type="instruction_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "stream_version_id": str(draft.stream_version_id),
            "version_number": draft.version_number,
            "reference": draft.reference,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft


async def published_instruction(
    session: AsyncSession, context: OrganizationContext, *, stream_version_id: uuid.UUID
) -> InstructionVersion | None:
    """The published instruction version an extraction call must
    reference — None means the stream runs without model instructions."""
    return await InstructionVersionRepository(session, context).get_published(stream_version_id)


__all__ = [
    "MAX_FIELD_GUIDANCE_ENTRIES",
    "MAX_INSTRUCTIONS_CHARS",
    "InstructionValidationError",
    "InstructionVersion",
    "InstructionVersionRepository",
    "create_instruction_draft",
    "publish_instruction_draft",
    "published_instruction",
    "update_instruction_draft",
    "validate_instruction_content",
]
