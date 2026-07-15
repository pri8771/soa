"""Durable worker execution for approved document-deletion requests."""

import uuid

from soa_db import DatabaseSessions
from soa_db.advisory import transaction_advisory_lock
from soa_db.audit import ActorType, record_audit_event
from soa_db.data_deletion import DeletionBlockedError, delete_document_data
from soa_db.deletion_requests import (
    DeletionRequestRepository,
    DeletionRequestState,
    LegalHoldRepository,
)
from soa_db.repository import OrganizationContext
from soa_db.retention import DeletionState
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow
from soa_storage import ObjectStore

ACTOR = "system:document-deletion"


async def execute_document_deletion(
    db: DatabaseSessions,
    store: ObjectStore,
    *,
    organization_id: uuid.UUID,
    deletion_request_id: uuid.UUID,
) -> str:
    """Execute one approved request and persist every domain transition.

    The running transition commits before external object I/O. A failed
    attempt is then recorded in a separate transaction, so the durable request
    never lies in ``running`` merely because its erasure transaction rolled
    back. Queue redelivery resumes ``failed`` requests idempotently.
    """

    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        await bind_tenant(session, organization_id)
        repository = DeletionRequestRepository(session, context)
        candidate = await repository.get(deletion_request_id)
        if candidate is None:
            raise ValueError("document deletion request does not exist")
        await transaction_advisory_lock(
            session, "document-deletion-lifecycle", organization_id, candidate.document_id
        )
        request = await repository.get(deletion_request_id, for_update=True)
        if request is None:
            raise ValueError("document deletion request does not exist")
        if request.state == DeletionRequestState.COMPLETED.value:
            return request.state
        if request.state == DeletionRequestState.PENDING_APPROVAL.value:
            # An approval can be revoked by a legal hold before the queue job
            # starts. The stale delivery is intentionally a no-op.
            return request.state
        if request.state == DeletionRequestState.CANCELLED.value:
            return request.state
        if request.state not in (
            DeletionRequestState.APPROVED.value,
            DeletionRequestState.FAILED.value,
            DeletionRequestState.RUNNING.value,
        ):
            raise ValueError(f"deletion request is not executable from {request.state!r}")
        if await LegalHoldRepository(session, context).active_for_document(
            request.document_id, for_update=True
        ):
            request.state = DeletionRequestState.PENDING_APPROVAL.value
            request.approved_by = None
            request.approval_reason = None
            request.approved_at = None
            request.safe_error = "approval cleared because an active legal hold blocks deletion"
            return request.state
        request.state = DeletionRequestState.RUNNING.value
        request.safe_error = None
        await record_audit_event(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR,
            action="document.deletion_started",
            target_type="document",
            target_id=str(request.document_id),
            organization_id=organization_id,
            summary={"deletion_request_id": str(request.id)},
        )

    try:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            repository = DeletionRequestRepository(session, context)
            candidate = await repository.get(deletion_request_id)
            if candidate is None:
                raise ValueError("document deletion request disappeared")
            await transaction_advisory_lock(
                session, "document-deletion-lifecycle", organization_id, candidate.document_id
            )
            request = await repository.get(deletion_request_id, for_update=True)
            if request is None:
                raise ValueError("document deletion request disappeared")
            if request.state == DeletionRequestState.COMPLETED.value:
                return request.state
            if request.state != DeletionRequestState.RUNNING.value:
                raise DeletionBlockedError(
                    f"deletion approval changed while executing ({request.state!r})"
                )
            if await LegalHoldRepository(session, context).active_for_document(
                request.document_id, for_update=True
            ):
                raise DeletionBlockedError("an active legal hold blocks document deletion")
            result = await delete_document_data(
                session,
                store,
                context,
                document_id=request.document_id,
                deletion_state=DeletionState.APPROVED,
                reason=request.reason,
                actor_id=ACTOR,
                requested_by=request.requested_by,
            )
            request.state = DeletionRequestState.COMPLETED.value
            request.safe_error = None
            request.completed_at = utcnow()
            await record_audit_event(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=ACTOR,
                action="document.deletion_completed",
                target_type="document",
                target_id=str(request.document_id),
                organization_id=organization_id,
                summary={
                    "deletion_request_id": str(request.id),
                    "tombstone_id": str(result.tombstone_id),
                    "object_keys_deleted": result.object_keys_deleted,
                    "category_counts": result.category_counts,
                },
            )
            return request.state
    except Exception as error:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            repository = DeletionRequestRepository(session, context)
            candidate = await repository.get(deletion_request_id)
            if candidate is None:
                raise
            await transaction_advisory_lock(
                session, "document-deletion-lifecycle", organization_id, candidate.document_id
            )
            failed = await repository.get(deletion_request_id, for_update=True)
            if failed is not None and failed.state == DeletionRequestState.RUNNING.value:
                failed.state = DeletionRequestState.FAILED.value
                failed.safe_error = f"document deletion failed ({type(error).__name__})"
                await record_audit_event(
                    session,
                    actor_type=ActorType.SYSTEM,
                    actor_id=ACTOR,
                    action="document.deletion_failed",
                    target_type="document",
                    target_id=str(failed.document_id),
                    organization_id=organization_id,
                    summary={
                        "deletion_request_id": str(failed.id),
                        "error_type": type(error).__name__,
                    },
                )
        raise


__all__ = ["execute_document_deletion"]
