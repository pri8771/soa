"""Job administration endpoints (JOB-006).

Tenant-facing: ``jobs.read`` lists and inspects the organization's own
jobs; ``jobs.manage`` replays dead-letter jobs and cancels pending ones,
both with a required, audited reason. Payloads are never exposed here —
responses carry operational metadata and the safe error summary only.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, get_dependencies
from soa_api.services.job_admin_service import (
    JobActionError,
    cancel_job,
    get_job,
    list_jobs,
    queue_stats,
    replay_job,
)
from soa_db import CursorRequest, InvalidCursorError, decode_cursor
from soa_db.jobs import Job, JobStatus

router = APIRouter(tags=["jobs"])


class JobResponse(BaseModel):
    """Operational view of a job. Deliberately NO payload field: payloads
    may contain document contents and are not part of the admin surface."""

    id: str
    job_type: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    run_after: str
    created_at: str
    finished_at: str | None
    last_error: str | None
    correlation_id: str | None

    @classmethod
    def from_model(cls, job: Job) -> "JobResponse":
        return cls(
            id=str(job.id),
            job_type=job.job_type,
            status=job.status,
            priority=job.priority,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            run_after=job.run_after.isoformat(),
            created_at=job.created_at.isoformat(),
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
            last_error=job.last_error,
            correlation_id=job.correlation_id,
        )


class JobsPageResponse(BaseModel):
    items: list[JobResponse]
    has_more: bool
    next_cursor: str | None


class JobActionRequest(BaseModel):
    """Replay/cancel both demand an operator-supplied reason for the audit
    trail — "why was this replayed" must survive the person who did it."""

    reason: str = Field(min_length=3, max_length=500)


@router.get("/orgs/{organization_slug}/jobs")
async def list_organization_jobs(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.read"))],
    session: DbSession,
    limit: int = 50,
    cursor: str | None = None,
    job_status: Annotated[JobStatus | None, "status filter"] = None,
) -> JobsPageResponse:
    try:
        after = decode_cursor(cursor) if cursor else None
        request = CursorRequest(limit=limit, after=after)
    except (InvalidCursorError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    page = await list_jobs(
        session,
        authorized.org_context,
        request,
        status_filter=str(job_status) if job_status is not None else None,
    )
    return JobsPageResponse(
        items=[JobResponse.from_model(job) for job in page.items],
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


class QueueStatsResponse(BaseModel):
    by_status: dict[str, int]
    oldest_pending_run_after: str | None


# NOTE: declared before /jobs/{job_id} so "stats" is not parsed as a job id.
@router.get("/orgs/{organization_slug}/jobs/stats")
async def get_queue_stats(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.read"))],
    session: DbSession,
) -> QueueStatsResponse:
    stats = await queue_stats(session, authorized.org_context)
    return QueueStatsResponse(
        by_status=stats.by_status,
        oldest_pending_run_after=(
            stats.oldest_pending_run_after.isoformat() if stats.oldest_pending_run_after else None
        ),
    )


@router.get("/orgs/{organization_slug}/jobs/{job_id}")
async def get_organization_job(
    job_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.read"))],
    session: DbSession,
) -> JobResponse:
    job = await get_job(session, authorized.org_context, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return JobResponse.from_model(job)


@router.post("/orgs/{organization_slug}/jobs/{job_id}/replay")
async def replay_organization_job(
    job_id: uuid.UUID,
    body: JobActionRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.manage"))],
    session: DbSession,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> JobResponse:
    # Abuse control (SEC-003): per-principal cap on replays.
    deps.rate_limiter.enforce(
        "replays",
        f"user:{authorized.membership.user_id}",
        deps.settings.rate_limit_replays_per_minute,
    )
    job = await get_job(session, authorized.org_context, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    try:
        job = await replay_job(
            session,
            authorized.org_context,
            job=job,
            reason=body.reason,
            actor_id=f"user:{authorized.membership.user_id}",
        )
    except JobActionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return JobResponse.from_model(job)


@router.post("/orgs/{organization_slug}/jobs/{job_id}/cancel")
async def cancel_organization_job(
    job_id: uuid.UUID,
    body: JobActionRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("jobs.manage"))],
    session: DbSession,
) -> JobResponse:
    job = await get_job(session, authorized.org_context, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    try:
        job = await cancel_job(
            session,
            authorized.org_context,
            job=job,
            reason=body.reason,
            actor_id=f"user:{authorized.membership.user_id}",
        )
    except JobActionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return JobResponse.from_model(job)
