"""Tenant-scoped job administration (JOB-006).

Every read and mutation here is bound to the caller's authorized
organization: system jobs (``organization_id IS NULL``) and other tenants'
jobs are invisible — the query filters on the tenant ID, so a guessed job
UUID resolves to "not found", never to data. Cross-tenant/system job
controls are reserved for the internal ``jobs.admin`` permission, which no
tenant role can hold or grant.

Replay and cancel are audited with the operator's required reason.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.audit import ActorType, record_audit_event
from soa_db.jobs import Job, JobStatus
from soa_db.pagination import CursorRequest, Page, build_page
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow


class JobActionError(Exception):
    """The job is not in a state that permits the requested action."""

    def __init__(self, *, job_id: uuid.UUID, action: str, status: str) -> None:
        self.job_id = job_id
        self.action = action
        self.status = status
        super().__init__(f"cannot {action} job {job_id} in state {status!r}")


async def list_jobs(
    session: AsyncSession,
    context: OrganizationContext,
    request: CursorRequest,
    *,
    status_filter: str | None = None,
) -> Page[Job]:
    stmt = (
        select(Job)
        .where(Job.organization_id == context.organization_id)
        .order_by(Job.id)
        .limit(request.limit + 1)
    )
    if status_filter is not None:
        stmt = stmt.where(Job.status == status_filter)
    if request.after is not None:
        stmt = stmt.where(Job.id > request.after)
    rows = list((await session.execute(stmt)).scalars().all())
    return build_page(rows, request.limit, id_of=lambda job: job.id)


async def get_job(
    session: AsyncSession, context: OrganizationContext, job_id: uuid.UUID
) -> Job | None:
    stmt = select(Job).where(Job.id == job_id, Job.organization_id == context.organization_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def replay_job(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    job: Job,
    reason: str,
    actor_id: str,
) -> Job:
    """Return a dead-letter job to the queue with a fresh attempt budget.

    Only dead-letter jobs replay — pending/running jobs are already live and
    terminal jobs stay terminal. The prior attempt count is preserved in the
    audit record; the counter resets so the replay gets real retries.
    """
    if job.status != JobStatus.DEAD_LETTER:
        raise JobActionError(job_id=job.id, action="replay", status=job.status)
    previous_attempts = job.attempts
    job.status = JobStatus.PENDING
    job.attempts = 0
    job.run_after = utcnow()
    job.finished_at = None
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="job.replayed",
        target_type="job",
        target_id=str(job.id),
        organization_id=context.organization_id,
        summary={
            "reason": reason,
            "job_type": job.job_type,
            "previous_attempts": previous_attempts,
            "previous_error": job.last_error,
        },
    )
    return job


async def cancel_job(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    job: Job,
    reason: str,
    actor_id: str,
) -> Job:
    """Cancel a pending job. Running jobs finish their current attempt —
    cancelling under a live worker would fight the lock owner."""
    if job.status != JobStatus.PENDING:
        raise JobActionError(job_id=job.id, action="cancel", status=job.status)
    job.status = JobStatus.CANCELLED
    job.finished_at = utcnow()
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="job.cancelled",
        target_type="job",
        target_id=str(job.id),
        organization_id=context.organization_id,
        summary={"reason": reason, "job_type": job.job_type},
    )
    return job
