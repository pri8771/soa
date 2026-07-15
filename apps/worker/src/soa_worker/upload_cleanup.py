"""Scheduled cleanup for abandoned direct-upload sessions."""

import uuid

from soa_db import DatabaseSessions
from soa_db.audit import ActorType, record_audit_event
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow
from soa_db.uploads import (
    UPLOAD_CLEANUP_JOB_TYPE,
    UPLOAD_VERIFICATION_LEASE,
    UploadSessionRepository,
    UploadSessionState,
    is_expired,
)
from soa_storage import ObjectNotFoundError, ObjectStore

ACTOR = "system:upload-cleanup"


class UploadCleanupNotDueError(RuntimeError):
    """Clock skew delivered a scheduled cleanup before its exact deadline."""


async def cleanup_expired_upload(
    db: DatabaseSessions,
    store: ObjectStore,
    *,
    organization_id: uuid.UUID,
    upload_session_id: uuid.UUID,
) -> str:
    """Expire one pending session and remove any bytes uploaded to its key.

    The row lock serializes cleanup with complete/abort. Redelivery is a
    no-op once another request has reached a terminal session state.
    """

    context = OrganizationContext(organization_id=organization_id)
    object_key: str
    state: str
    async with db.session_scope() as session:
        await bind_tenant(session, organization_id)
        record = await UploadSessionRepository(session, context).get(
            upload_session_id, for_update=True
        )
        if record is None:
            return "missing"
        if record.state == UploadSessionState.COMPLETED.value:
            return record.state
        if record.state == UploadSessionState.PENDING.value and not is_expired(record):
            # Do not acknowledge the only cleanup intent early. The generic
            # queue retries runtime failures with a bounded backoff.
            raise UploadCleanupNotDueError("upload cleanup ran before session expiry")
        if (
            record.state == UploadSessionState.VERIFYING.value
            and record.verification_started_at is not None
            and record.verification_started_at + UPLOAD_VERIFICATION_LEASE > utcnow()
        ):
            raise UploadCleanupNotDueError("upload verification lease is still active")
        if record.state in (
            UploadSessionState.PENDING.value,
            UploadSessionState.VERIFYING.value,
        ):
            record.state = UploadSessionState.EXPIRED.value
            record.verification_started_at = None
            record.verification_token = None
            await session.flush()
            await record_audit_event(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=ACTOR,
                action="upload_session.expired",
                target_type="upload_session",
                target_id=str(record.id),
                organization_id=organization_id,
                summary={"object_cleanup": "scheduled"},
            )
        object_key = record.object_key
        state = record.state

    # The terminal state now fences complete/abort. Storage deletion runs
    # without a row lock or SQL connection; failures propagate so the durable
    # cleanup job retries even when the row is already expired.
    object_deleted = True
    try:
        await store.delete(object_key)
    except ObjectNotFoundError:
        object_deleted = False

    async with db.session_scope() as session:
        await bind_tenant(session, organization_id)
        await record_audit_event(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR,
            action="upload_session.object_cleanup_completed",
            target_type="upload_session",
            target_id=str(upload_session_id),
            organization_id=organization_id,
            summary={"object_deleted": object_deleted, "state": state},
        )
    return state


__all__ = [
    "UPLOAD_CLEANUP_JOB_TYPE",
    "UploadCleanupNotDueError",
    "cleanup_expired_upload",
]
