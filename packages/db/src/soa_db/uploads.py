"""Durable upload-session state shared by the API and cleanup worker.

The browser upload itself is direct-to-object-storage, but its declaration,
preallocated document id, expiry, and lifecycle are database state. Keeping
that state in ``soa-db`` lets the worker clean abandoned objects without
depending on the HTTP application package.
"""

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import String, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.advisory import transaction_advisory_lock
from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow, uuid7

UPLOAD_CLEANUP_JOB_TYPE = "upload.cleanup"
# A signed request may begin immediately before capability expiry and finish
# later. The primary pass covers normal in-flight writes; the second pass
# catches unusually slow finalization and post-abort writes made with an
# already-issued URL (signed capabilities cannot be individually revoked).
UPLOAD_CLEANUP_GRACE = timedelta(hours=1)
UPLOAD_CLEANUP_SWEEP_GRACE = timedelta(hours=24)
UPLOAD_VERIFICATION_LEASE = timedelta(minutes=5)


class UploadSessionState(StrEnum):
    PENDING = "pending"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class UploadSession(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "upload_sessions"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
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
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    verification_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    verification_token: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)


_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    UploadSessionState.PENDING.value: frozenset(
        {
            UploadSessionState.COMPLETED.value,
            UploadSessionState.VERIFYING.value,
            UploadSessionState.ABORTED.value,
            UploadSessionState.EXPIRED.value,
        }
    ),
    UploadSessionState.VERIFYING.value: frozenset(
        {
            UploadSessionState.PENDING.value,
            UploadSessionState.COMPLETED.value,
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

    async def lock_pending_quota(self) -> None:
        """Serialize the per-organization pending-count/create decision.

        A row lock cannot protect an empty set. PostgreSQL therefore uses a
        transaction-scoped advisory lock derived from the tenant UUID; SQLite
        is single-process test/local storage and needs no equivalent. The lock
        is held until the request unit of work commits.
        """

        await transaction_advisory_lock(
            self._session,
            "upload-pending-quota",
            self._context.organization_id,
        )

    async def count_pending(self) -> int:
        stmt = (
            select(func.count())
            .select_from(UploadSession)
            .where(
                UploadSession.organization_id == self._context.organization_id,
                UploadSession.state == UploadSessionState.PENDING.value,
                # An expired declaration is no longer an active client slot.
                # Its object is removed by the scheduled cleanup job.
                UploadSession.expires_at > utcnow(),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())


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
    return (now or utcnow()) >= record.expires_at


__all__ = [
    "UPLOAD_CLEANUP_GRACE",
    "UPLOAD_CLEANUP_JOB_TYPE",
    "UPLOAD_CLEANUP_SWEEP_GRACE",
    "UPLOAD_VERIFICATION_LEASE",
    "InvalidUploadSessionStateError",
    "UploadSession",
    "UploadSessionRepository",
    "UploadSessionState",
    "create_upload_session",
    "is_expired",
]
