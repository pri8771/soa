"""Operator-initiated document data deletion (SEC-010 API surface).

One endpoint: permanently erase everything the platform stores for one
document — stored objects, rendered pages, extracted fields, runs and
stage attempts, review tasks and comments, corrections, canonical
payloads, and export jobs — through the SEC-010 deletion workflow. The
workflow reconciles that the objects are actually gone, keeps the
document row and the audit trail (a deletion must stay attributable),
and leaves a retained :class:`~soa_db.data_deletion.DeletionTombstone`
plus the counts-only audit event it writes itself.

This is the OPERATOR-INITIATED path: the ``data.delete`` permission on a
membership in the document's organization constitutes the SEC-008
operator sign-off, so this endpoint passes
:attr:`~soa_db.retention.DeletionState.APPROVED` directly to
:func:`~soa_db.data_deletion.delete_document_data`. The
retention-SCHEDULED path — engine-driven eligibility plus a separate
approval queue — remains the SEC-008/010 follow-on and is deliberately
not faked here.

Documents still moving through the pipeline are refused with 409:
cancel processing first, then delete. Repeat calls are idempotent — the
workflow returns the existing completed tombstone and the state write
is skipped.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, ObjectStoreDep, get_dependencies
from soa_db.audit import ActorType
from soa_db.data_deletion import delete_document_data
from soa_db.documents import DocumentRepository, DocumentState, transition_document
from soa_db.retention import DeletionState

router = APIRouter(tags=["deletions"])

#: States a document may be deleted from — everything that is settled or
#: waiting on a human, i.e. NOT actively moving through the pipeline.
DELETABLE_STATES: frozenset[str] = frozenset(
    state.value
    for state in (
        DocumentState.REVIEW_REQUIRED,
        DocumentState.APPROVED,
        DocumentState.COMPLETED,
        DocumentState.REJECTED,
        DocumentState.QUARANTINED,
        DocumentState.FAILED_RETRYABLE,
        DocumentState.FAILED_TERMINAL,
        DocumentState.CANCELLED,
        DocumentState.ARCHIVED,
    )
)


class DeletionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post(
    "/orgs/{organization_slug}/documents/{document_id}/deletion",
    status_code=status.HTTP_201_CREATED,
)
async def delete_document(
    document_id: uuid.UUID,
    body: DeletionRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.delete"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    """Erase one document's stored data and settle it as ``deleted``.

    Holding ``data.delete`` IS the SEC-008 operator sign-off for this
    operator-initiated path, so the SEC-010 workflow runs with
    ``DeletionState.APPROVED``; the retention-scheduled path (engine
    eligibility + approval queue) is a separate SEC-008/010 follow-on.
    The workflow writes the deletion audit event itself — counts only.
    """
    # Abuse control (SEC-003): per-principal cap on deletions.
    deps.rate_limiter.enforce(
        "deletion",
        f"user:{authorized.membership.user_id}",
        deps.settings.rate_limit_deletions_per_minute,
    )
    # Tenant scope first: a document outside the caller's organization is
    # indistinguishable from one that does not exist.
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    already_deleted = document.state == DocumentState.DELETED.value
    if not already_deleted and document.state not in DELETABLE_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The document is still being processed (state {document.state!r}); "
                "cancel processing first, then delete."
            ),
        )

    actor_id = f"user:{authorized.membership.user_id}"
    result = await delete_document_data(
        session,
        store,
        authorized.org_context,
        document_id=document_id,
        deletion_state=DeletionState.APPROVED,
        reason=body.reason,
        actor_id=actor_id,
    )
    # First deletion settles the document as deleted, carrying the
    # operator's reason. A repeat call finds the completed tombstone AND
    # the deleted state — nothing to write, so the call stays idempotent
    # (no duplicate state-change audit event).
    if not already_deleted:
        await transition_document(
            session,
            authorized.org_context,
            document=document,
            to_state=DocumentState.DELETED,
            reason=body.reason,
            actor_id=actor_id,
            actor_type=ActorType.USER,
        )
    return {
        "tombstone_id": str(result.tombstone_id),
        "document_id": str(result.document_id),
        "object_keys_deleted": result.object_keys_deleted,
        "category_counts": result.category_counts,
        "already_complete": result.already_complete,
    }
