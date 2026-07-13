"""Delivery history endpoints (EXP-009).

Reads need ``integrations.read``; the retry/replay controls need
``integrations.replay`` — deliberately separated so audit-style roles
can inspect every delivery without being able to fire one.

What history shows is exactly what was stored: job state, pinned
mapping version, canonical payload reference, and append-only attempts
with status + SAFE error + request hash. Raw responses and request
headers were never persisted (EXP-007 stores redacted excerpts at
most), so no secret header can be displayed here by construction — and
a test greps the responses anyway.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, get_dependencies
from soa_api.services.export_orchestration import EXPORT_JOB_TYPE
from soa_db.audit import ActorType, record_audit_event
from soa_db.exports import (
    DeliveryAttempt,
    DeliveryAttemptRepository,
    ExportJob,
    ExportJobRepository,
    ExportJobState,
    InvalidExportTransitionError,
    replay_export_job,
    transition_export_job,
)
from soa_db.integrations import Integration, IntegrationRepository, MappingProfileVersionRepository
from soa_db.jobs import enqueue_job

router = APIRouter(tags=["exports"])


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


def _serialize_job(job: ExportJob, integration: Integration | None) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "run_id": str(job.run_id),
        "canonical_payload_id": str(job.canonical_payload_id),
        "integration_id": str(job.integration_id),
        "integration_slug": integration.slug if integration else None,
        "integration_name": integration.name if integration else None,
        "mapping_version_id": str(job.mapping_version_id),
        "business_key": job.business_key,
        "state": job.state,
        "attempt_count": job.attempt_count,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _serialize_attempt(attempt: DeliveryAttempt) -> dict[str, Any]:
    return {
        "attempt_number": attempt.attempt_number,
        "outcome": attempt.outcome,
        "response_status": attempt.response_status,
        "safe_error": attempt.safe_error,
        "request_sha256": attempt.request_sha256,
        "started_at": attempt.started_at.isoformat(),
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
    }


async def _load_job(
    session: DbSession, authorized: AuthorizedContext, export_job_id: uuid.UUID
) -> ExportJob:
    job = await ExportJobRepository(session, authorized.org_context).get(export_job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not found.")
    return job


@router.get("/orgs/{organization_slug}/exports")
async def list_exports(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.read"))],
    session: DbSession,
    document_id: Annotated[uuid.UUID | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    if state is not None and state not in {s.value for s in ExportJobState}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown state.")
    repo = ExportJobRepository(session, authorized.org_context)
    stmt = repo._scoped_select().order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
    if document_id is not None:
        stmt = stmt.where(ExportJob.document_id == document_id)
    if state is not None:
        stmt = stmt.where(ExportJob.state == state)
    jobs = list((await session.execute(stmt.limit(200))).scalars().all())
    integrations: dict[uuid.UUID, Integration] = {}
    integration_ids = {job.integration_id for job in jobs}
    if integration_ids:
        rows = (
            (
                await session.execute(
                    select(Integration).where(
                        Integration.organization_id == authorized.org_context.organization_id,
                        Integration.id.in_(integration_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        integrations = {row.id: row for row in rows}
    return {"items": [_serialize_job(job, integrations.get(job.integration_id)) for job in jobs]}


@router.get("/orgs/{organization_slug}/exports/{export_job_id}")
async def get_export(
    export_job_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.read"))],
    session: DbSession,
) -> dict[str, Any]:
    job = await _load_job(session, authorized, export_job_id)
    integration = await IntegrationRepository(session, authorized.org_context).get(
        job.integration_id
    )
    mapping = await MappingProfileVersionRepository(session, authorized.org_context).get(
        job.mapping_version_id
    )
    attempts = await DeliveryAttemptRepository(session, authorized.org_context).list_for_job(job.id)
    return {
        "job": _serialize_job(job, integration),
        "mapping_version_number": mapping.version_number if mapping else None,
        "attempts": [_serialize_attempt(attempt) for attempt in attempts],
    }


async def _enqueue_delivery(
    session: DbSession, authorized: AuthorizedContext, job: ExportJob, *, reason_tag: str
) -> None:
    await enqueue_job(
        session,
        job_type=EXPORT_JOB_TYPE,
        payload={
            "export_job_id": str(job.id),
            "organization_id": str(authorized.org_context.organization_id),
        },
        organization_id=authorized.org_context.organization_id,
        # Distinct per attempt count: the original delivery job may have
        # settled, and dedupe returns existing jobs in ANY state.
        dedupe_key=f"{EXPORT_JOB_TYPE}:{job.id}:{reason_tag}:{job.attempt_count}",
    )


@router.post("/orgs/{organization_slug}/exports/{export_job_id}/retry")
async def retry_export(
    export_job_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.replay"))],
    session: DbSession,
) -> dict[str, Any]:
    """Re-queue a RETRYABLE failure. Same job, same payload, same
    business key — the receiver sees a redelivery, not a new order."""
    job = await _load_job(session, authorized, export_job_id)
    if job.state != ExportJobState.FAILED_RETRYABLE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only retryable failures can be retried; this export is {job.state}. "
                "Settled exports replay with an audited reason."
            ),
        )
    try:
        await transition_export_job(
            session,
            authorized.org_context,
            job=job,
            to_state=ExportJobState.PENDING,
            actor_id=_actor(authorized),
            reason="operator retry",
        )
    except InvalidExportTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await _enqueue_delivery(session, authorized, job, reason_tag="retry")
    return _serialize_job(job, None)


class ReplayRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/orgs/{organization_slug}/exports/{export_job_id}/replay")
async def replay_export(
    export_job_id: uuid.UUID,
    body: ReplayRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.replay"))],
    session: DbSession,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    """Replay a SETTLED (failed_terminal/cancelled) export with an
    audited reason. Nothing is re-extracted; the fixed payload
    redelivers under the same business key."""
    # Abuse control (SEC-003): per-principal cap on replays.
    deps.rate_limiter.enforce(
        "replays",
        f"user:{authorized.membership.user_id}",
        deps.settings.rate_limit_replays_per_minute,
    )
    job = await _load_job(session, authorized, export_job_id)
    try:
        await replay_export_job(
            session,
            authorized.org_context,
            job=job,
            actor_id=_actor(authorized),
            reason=body.reason,
        )
    except InvalidExportTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    await _enqueue_delivery(session, authorized, job, reason_tag="replay")
    return _serialize_job(job, None)


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/orgs/{organization_slug}/exports/{export_job_id}/cancel")
async def cancel_export(
    export_job_id: uuid.UUID,
    body: CancelRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("integrations.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    job = await _load_job(session, authorized, export_job_id)
    try:
        await transition_export_job(
            session,
            authorized.org_context,
            job=job,
            to_state=ExportJobState.CANCELLED,
            actor_id=_actor(authorized),
            reason=body.reason,
        )
    except InvalidExportTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=_actor(authorized),
        action="export_job.cancelled",
        target_type="export_job",
        target_id=str(job.id),
        organization_id=authorized.org_context.organization_id,
        summary={"reason": body.reason},
    )
    return _serialize_job(job, None)
