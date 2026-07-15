"""Approval-gated document erasure and legal-hold administration."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_db.deletion_requests import (
    DeletionLifecycleError,
    DeletionRequest,
    DeletionRequestRepository,
    LegalHold,
    LegalHoldRepository,
    approve_document_deletion,
    cancel_document_deletion,
    place_legal_hold,
    release_legal_hold,
    request_document_deletion,
)

router = APIRouter(tags=["data-deletion"])


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class DeletionRequestResponse(BaseModel):
    id: str
    document_id: str
    state: str
    reason: str
    requested_by: str
    requested_at: str
    approved_by: str | None
    approval_reason: str | None
    approved_at: str | None
    completed_at: str | None
    cancelled_by: str | None
    cancellation_reason: str | None
    cancelled_at: str | None
    safe_error: str | None
    version: int

    @classmethod
    def from_model(cls, request: DeletionRequest) -> "DeletionRequestResponse":
        return cls(
            id=str(request.id),
            document_id=str(request.document_id),
            state=request.state,
            reason=request.reason,
            requested_by=request.requested_by,
            requested_at=request.requested_at.isoformat(),
            approved_by=request.approved_by,
            approval_reason=request.approval_reason,
            approved_at=request.approved_at.isoformat() if request.approved_at else None,
            completed_at=request.completed_at.isoformat() if request.completed_at else None,
            cancelled_by=request.cancelled_by,
            cancellation_reason=request.cancellation_reason,
            cancelled_at=request.cancelled_at.isoformat() if request.cancelled_at else None,
            safe_error=request.safe_error,
            version=request.version,
        )


class LegalHoldResponse(BaseModel):
    id: str
    document_id: str
    state: str
    reason: str
    placed_by: str
    placed_at: str
    released_by: str | None
    release_reason: str | None
    released_at: str | None
    version: int

    @classmethod
    def from_model(cls, hold: LegalHold) -> "LegalHoldResponse":
        return cls(
            id=str(hold.id),
            document_id=str(hold.document_id),
            state=hold.state,
            reason=hold.reason,
            placed_by=hold.placed_by,
            placed_at=hold.placed_at.isoformat(),
            released_by=hold.released_by,
            release_reason=hold.release_reason,
            released_at=hold.released_at.isoformat() if hold.released_at else None,
            version=hold.version,
        )


@router.post(
    "/orgs/{organization_slug}/documents/{document_id}/deletion-requests",
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_deletion_request(
    document_id: uuid.UUID,
    body: ReasonRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.delete.request"))],
    session: DbSession,
) -> DeletionRequestResponse:
    try:
        request = await request_document_deletion(
            session,
            authorized.org_context,
            document_id=document_id,
            reason=body.reason,
            actor_id=authorized.principal.subject,
        )
    except DeletionLifecycleError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return DeletionRequestResponse.from_model(request)


@router.post("/orgs/{organization_slug}/deletion-requests/{request_id}/cancel")
async def cancel_deletion_request(
    request_id: uuid.UUID,
    body: ReasonRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.delete.request"))],
    session: DbSession,
) -> DeletionRequestResponse:
    request = await DeletionRequestRepository(session, authorized.org_context).get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Deletion request not found.")
    try:
        request = await cancel_document_deletion(
            session,
            authorized.org_context,
            request=request,
            reason=body.reason,
            actor_id=authorized.principal.subject,
        )
    except DeletionLifecycleError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return DeletionRequestResponse.from_model(request)


@router.get("/orgs/{organization_slug}/deletion-requests")
async def list_deletion_requests(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.delete.request"))],
    session: DbSession,
    limit: int = 100,
) -> dict[str, list[DeletionRequestResponse]]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    requests = await DeletionRequestRepository(session, authorized.org_context).list_recent(
        limit=limit
    )
    return {"items": [DeletionRequestResponse.from_model(request) for request in requests]}


@router.post(
    "/orgs/{organization_slug}/deletion-requests/{request_id}/approve",
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_deletion_request(
    request_id: uuid.UUID,
    body: ReasonRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.delete.approve"))],
    session: DbSession,
) -> DeletionRequestResponse:
    request = await DeletionRequestRepository(session, authorized.org_context).get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Deletion request not found.")
    try:
        request = await approve_document_deletion(
            session,
            authorized.org_context,
            request=request,
            approval_reason=body.reason,
            actor_id=authorized.principal.subject,
        )
    except DeletionLifecycleError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return DeletionRequestResponse.from_model(request)


@router.post(
    "/orgs/{organization_slug}/documents/{document_id}/legal-holds",
    status_code=status.HTTP_201_CREATED,
)
async def create_legal_hold(
    document_id: uuid.UUID,
    body: ReasonRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.retention.manage"))],
    session: DbSession,
) -> LegalHoldResponse:
    try:
        hold = await place_legal_hold(
            session,
            authorized.org_context,
            document_id=document_id,
            reason=body.reason,
            actor_id=authorized.principal.subject,
        )
    except DeletionLifecycleError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return LegalHoldResponse.from_model(hold)


@router.get("/orgs/{organization_slug}/documents/{document_id}/legal-holds")
async def list_legal_holds(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.retention.manage"))],
    session: DbSession,
) -> dict[str, list[LegalHoldResponse]]:
    holds = await LegalHoldRepository(session, authorized.org_context).list_for_document(
        document_id
    )
    return {"items": [LegalHoldResponse.from_model(hold) for hold in holds]}


@router.post("/orgs/{organization_slug}/legal-holds/{hold_id}/release")
async def release_document_legal_hold(
    hold_id: uuid.UUID,
    body: ReasonRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.retention.manage"))],
    session: DbSession,
) -> LegalHoldResponse:
    hold = await LegalHoldRepository(session, authorized.org_context).get(hold_id)
    if hold is None:
        raise HTTPException(status_code=404, detail="Legal hold not found.")
    try:
        hold = await release_legal_hold(
            session,
            authorized.org_context,
            hold=hold,
            reason=body.reason,
            actor_id=authorized.principal.subject,
        )
    except DeletionLifecycleError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    return LegalHoldResponse.from_model(hold)


__all__ = ["router"]
