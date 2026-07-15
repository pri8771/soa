"""Document and input-source models (ING-001).

A document is one customer file moving through the pipeline. Its state
machine is the canonical one from ARCHITECTURE §7 and is CONSTRAINED at
the ORM layer: a state change that isn't in the transition map refuses to
flush, whether it came through the helper or a raw attribute write.
State changes are audited with actor and reason.

Source metadata (who sent it, via which channel) passes through an
allowlist before storage: only known keys survive, values are truncated,
and nothing resembling raw message bodies or header dumps can land in
the row — that content belongs in artifacts with retention classes, not
in metadata columns.
"""

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Index, String, event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class SourceChannel(StrEnum):
    UPLOAD = "upload"  # interactive upload session (ING-002/008)
    API = "api"  # service-credential ingestion (ING-013)
    EMAIL = "email"  # inbound email intake (ING-014)


class DocumentState(StrEnum):
    # Happy path (ARCHITECTURE §7).
    RECEIVED = "received"
    VALIDATING_FILE = "validating_file"
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    CLASSIFYING = "classifying"
    SPLITTING = "splitting"
    EXTRACTING = "extracting"
    NORMALIZING = "normalizing"
    VALIDATING_DATA = "validating_data"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    # Exceptional states.
    QUARANTINED = "quarantined"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


_HAPPY_CHAIN: list[DocumentState] = [
    DocumentState.RECEIVED,
    DocumentState.VALIDATING_FILE,
    DocumentState.QUEUED,
    DocumentState.PREPROCESSING,
    DocumentState.CLASSIFYING,
    DocumentState.SPLITTING,
    DocumentState.EXTRACTING,
    DocumentState.NORMALIZING,
    DocumentState.VALIDATING_DATA,
]

#: Every allowed from -> to edge. Anything absent is refused at flush.
ALLOWED_TRANSITIONS: dict[DocumentState, frozenset[DocumentState]] = {}


def _allow(source: DocumentState, *targets: DocumentState) -> None:
    ALLOWED_TRANSITIONS[source] = ALLOWED_TRANSITIONS.get(source, frozenset()) | frozenset(targets)


for _index, _state in enumerate(_HAPPY_CHAIN[:-1]):
    _allow(_state, _HAPPY_CHAIN[_index + 1])
_allow(DocumentState.VALIDATING_DATA, DocumentState.REVIEW_REQUIRED, DocumentState.APPROVED)
_allow(DocumentState.REVIEW_REQUIRED, DocumentState.APPROVED, DocumentState.REJECTED)
_allow(DocumentState.APPROVED, DocumentState.EXPORTING)
_allow(DocumentState.EXPORTING, DocumentState.COMPLETED)

# File validation is where unsafe or unsupported files leave the pipeline.
_allow(DocumentState.VALIDATING_FILE, DocumentState.QUARANTINED, DocumentState.REJECTED)

# Any active state can fail (retryable or terminal) or be cancelled.
for _state in (*_HAPPY_CHAIN, DocumentState.REVIEW_REQUIRED, DocumentState.EXPORTING):
    _allow(
        _state,
        DocumentState.FAILED_RETRYABLE,
        DocumentState.FAILED_TERMINAL,
        DocumentState.CANCELLED,
    )

# Retryable failures re-enter the queue; retries that keep failing go
# terminal.
_allow(DocumentState.FAILED_RETRYABLE, DocumentState.QUEUED, DocumentState.FAILED_TERMINAL)

# Authorized reprocessing (PRC-013) re-queues a document for a NEW run:
# from review (send back under a corrected configuration) and from a
# terminal failure (the config that made it terminal may have been fixed).
# Approved/exporting/completed documents are protected by policy at the
# API layer and are deliberately NOT re-queueable here either.
_allow(DocumentState.REVIEW_REQUIRED, DocumentState.QUEUED)
_allow(DocumentState.FAILED_TERMINAL, DocumentState.QUEUED)

# Settled documents can be archived; archived is the single terminal state.
for _state in (
    DocumentState.COMPLETED,
    DocumentState.REJECTED,
    DocumentState.CANCELLED,
    DocumentState.FAILED_TERMINAL,
    DocumentState.QUARANTINED,
):
    _allow(_state, DocumentState.ARCHIVED)


class InvalidDocumentTransitionError(Exception):
    def __init__(self, document_id: object, source: str, target: str) -> None:
        super().__init__(
            f"document {document_id}: transition {source!r} -> {target!r} is not allowed"
        )


#: Allowlisted source-metadata keys per channel-neutral contract. Values
#: are truncated to this many characters; unknown keys are dropped.
_SOURCE_METADATA_KEYS = frozenset(
    {"sender", "subject", "message_id", "uploader", "api_client", "original_source"}
)
_SOURCE_METADATA_MAX_LENGTH = 300


def sanitize_source_metadata(raw: dict[str, Any] | None) -> dict[str, str]:
    """Keep only allowlisted keys with bounded string values. Everything
    else (bodies, header dumps, nested structures) is dropped, not stored."""
    if not raw:
        return {}
    sanitized: dict[str, str] = {}
    for key in sorted(_SOURCE_METADATA_KEYS & raw.keys()):
        value = raw[key]
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            sanitized[key] = str(value)[:_SOURCE_METADATA_MAX_LENGTH]
    return sanitized


class Document(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "documents"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        String(30), nullable=False, default=DocumentState.RECEIVED.value
    )
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Client-supplied idempotency reference: at most one document per
    # (organization, stream, reference) — enforced by a partial unique
    # index so documents without a reference never collide.
    client_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    priority: Mapped[int] = mapped_column(nullable=False, default=100)
    sla_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        PORTABLE_JSON, nullable=False, default=dict
    )
    # Why the document sits in an exceptional state, if it does.
    state_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Exact-content duplicate marker (ING-006): the earliest document in
    # the same stream with identical bytes. Set whatever the policy is —
    # duplicates are never silent, even when allowed to proceed.
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    __table_args__ = (
        Index(
            "uq_documents_client_reference",
            "organization_id",
            "stream_id",
            "client_reference",
            unique=True,
            postgresql_where=text("client_reference IS NOT NULL"),
            sqlite_where=text("client_reference IS NOT NULL"),
        ),
        Index("ix_documents_org_sha256", "organization_id", "content_sha256"),
        Index("ix_documents_org_state", "organization_id", "state"),
    )


@event.listens_for(Session, "before_flush")
def _constrain_document_transitions(session: Session, _ctx: object, _instances: object) -> None:
    from sqlalchemy import inspect as sa_inspect

    for entity in session.dirty:
        if not isinstance(entity, Document) or not session.is_modified(entity):
            continue
        history = sa_inspect(entity).attrs["state"].history
        if not history.has_changes():
            continue
        old = str(history.deleted[0]) if history.deleted else None
        new = str(entity.state)
        if old is None or old == new:
            continue
        allowed = ALLOWED_TRANSITIONS.get(DocumentState(old), frozenset())
        if DocumentState(new) not in allowed:
            raise InvalidDocumentTransitionError(entity.id, old, new)


class DocumentRepository(ScopedRepository[Document]):
    model = Document

    async def get_by_client_reference(
        self, stream_id: uuid.UUID, client_reference: str
    ) -> Document | None:
        stmt = self._scoped_select().where(
            Document.stream_id == stream_id,
            Document.client_reference == client_reference,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_content_hash(self, content_sha256: str) -> list[Document]:
        stmt = (
            self._scoped_select()
            .where(Document.content_sha256 == content_sha256)
            .order_by(Document.received_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_many(self, document_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Document]:
        if not document_ids:
            return {}
        stmt = self._scoped_select().where(Document.id.in_(document_ids))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}


async def create_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID | None = None,
    stream_id: uuid.UUID,
    source_channel: SourceChannel,
    original_filename: str,
    content_sha256: str,
    size_bytes: int,
    content_type: str,
    client_reference: str | None = None,
    priority: int = 100,
    sla_due_at: datetime | None = None,
    source_metadata: dict[str, Any] | None = None,
    actor_type: ActorType = ActorType.USER,
    actor_id: str,
) -> Document:
    if not _SHA256_HEX.fullmatch(content_sha256):
        raise ValueError("content_sha256 must be 64 lowercase hex characters")
    if size_bytes <= 0:
        raise ValueError("size_bytes must be positive")
    fields: dict[str, Any] = {"id": document_id} if document_id is not None else {}
    document = DocumentRepository(session, context).add(
        Document(
            **fields,
            stream_id=stream_id,
            source_channel=source_channel.value,
            original_filename=original_filename[:255],
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            client_reference=client_reference,
            priority=priority,
            sla_due_at=sla_due_at,
            source_metadata=sanitize_source_metadata(source_metadata),
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="document.received",
        target_type="document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        summary={
            "stream_id": str(stream_id),
            "source_channel": source_channel.value,
            "size_bytes": size_bytes,
        },
    )
    return document


async def transition_document(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    to_state: DocumentState,
    reason: str | None = None,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str,
    correlation_id: str | None = None,
) -> Document:
    """The audited way to move a document. The flush guard enforces the
    same transition map for anyone assigning ``state`` directly. The
    correlation id defaults to the ambient request/job correlation and can
    be pinned explicitly by orchestration code (PRC-002)."""
    from_state = document.state
    document.state = to_state.value
    document.state_reason = reason
    await session.flush()  # the guard validates here
    await record_audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="document.state_changed",
        target_type="document",
        target_id=str(document.id),
        organization_id=context.organization_id,
        correlation_id=correlation_id,
        summary={"from": from_state, "to": to_state.value, "reason": reason},
    )
    return document
