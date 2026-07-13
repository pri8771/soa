"""Upload sessions (ING-002).

An upload session is the contract between a client that wants to send a
file and the platform: the client declares filename, type, size, and
SHA-256 up front; the platform validates policy (supported type, size
cap, pending-session quota) BEFORE signing anything; the client PUTs the
bytes to a short-lived signed URL; completion verifies the stored
object's metadata against the declaration and registers the document
exactly once. Sessions expire lazily — a session past its deadline flips
to expired the moment anyone touches it.

The document id is allocated at session creation so the object key is
final from the start (tenant/document-scoped, non-guessable) — no
copy-on-complete, no orphan renames.
"""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import String, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow, uuid7

#: Types accepted for upload. Deep content inspection (signature vs
#: extension, ING-003) happens later in the pipeline; this is the intake
#: policy gate.
SUPPORTED_UPLOAD_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
    }
)


class UploadSessionState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class UploadPolicyError(Exception):
    """The declaration violates intake policy (type/size/quota)."""


class UploadSession(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "upload_sessions"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    #: Pre-allocated: becomes the Document id at completion.
    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=UploadSessionState.PENDING.value
    )
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    declared_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    declared_size_bytes: Mapped[int] = mapped_column(nullable=False)
    declared_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    client_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Set when completion registers the document.
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    UploadSessionState.PENDING.value: frozenset(
        {
            UploadSessionState.COMPLETED.value,
            UploadSessionState.ABORTED.value,
            UploadSessionState.EXPIRED.value,
        }
    ),
    UploadSessionState.COMPLETED.value: frozenset(),
    UploadSessionState.ABORTED.value: frozenset(),
    UploadSessionState.EXPIRED.value: frozenset(),
}


class InvalidUploadSessionStateError(Exception):
    def __init__(self, session_id: object, source: str, target: str) -> None:
        super().__init__(
            f"upload session {session_id}: transition {source!r} -> {target!r} is not allowed"
        )


@event.listens_for(Session, "before_flush")
def _constrain_session_transitions(session: Session, _ctx: object, _instances: object) -> None:
    from sqlalchemy import inspect as sa_inspect

    for entity in session.dirty:
        if not isinstance(entity, UploadSession) or not session.is_modified(entity):
            continue
        history = sa_inspect(entity).attrs["state"].history
        if not history.has_changes() or not history.deleted:
            continue
        old = str(history.deleted[0])
        new = str(entity.state)
        if old != new and new not in _SESSION_TRANSITIONS.get(old, frozenset()):
            raise InvalidUploadSessionStateError(entity.id, old, new)


class UploadSessionRepository(ScopedRepository[UploadSession]):
    model = UploadSession

    async def count_pending(self) -> int:
        from sqlalchemy import func, select

        stmt = (
            select(func.count())
            .select_from(UploadSession)
            .where(
                UploadSession.organization_id == self._context.organization_id,
                UploadSession.state == UploadSessionState.PENDING.value,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())


def validate_upload_declaration(
    *,
    content_type: str,
    size_bytes: int,
    pending_sessions: int,
    max_size_bytes: int,
    max_pending_sessions: int,
) -> None:
    """Policy gate, evaluated BEFORE any URL is signed."""
    if content_type not in SUPPORTED_UPLOAD_TYPES:
        supported = ", ".join(sorted(SUPPORTED_UPLOAD_TYPES))
        raise UploadPolicyError(
            f"unsupported content type {content_type!r}; supported: {supported}"
        )
    if size_bytes <= 0:
        raise UploadPolicyError("declared size must be positive")
    if size_bytes > max_size_bytes:
        raise UploadPolicyError(
            f"declared size {size_bytes} exceeds the {max_size_bytes}-byte limit"
        )
    if pending_sessions >= max_pending_sessions:
        raise UploadPolicyError(
            f"too many pending upload sessions ({pending_sessions}); "
            "complete or abort existing sessions first"
        )


async def create_upload_session(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    object_key: str,
    document_id: uuid.UUID | None = None,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str,
    client_reference: str | None,
    ttl_seconds: int,
    actor_id: str,
) -> UploadSession:
    record = UploadSessionRepository(session, context).add(
        UploadSession(
            stream_id=stream_id,
            document_id=document_id or uuid7(),
            object_key=object_key,
            declared_filename=filename[:255],
            declared_content_type=content_type,
            declared_size_bytes=size_bytes,
            declared_sha256=sha256,
            client_reference=client_reference,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
            created_by=actor_id,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="upload_session.created",
        target_type="upload_session",
        target_id=str(record.id),
        organization_id=context.organization_id,
        summary={
            "stream_id": str(stream_id),
            "declared_size_bytes": size_bytes,
            "declared_content_type": content_type,
            "client_reference": client_reference,
        },
    )
    return record


def is_expired(record: UploadSession, *, now: datetime | None = None) -> bool:
    return (now or utcnow()) > record.expires_at
