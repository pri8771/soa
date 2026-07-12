"""Durable job schema and state machine (JOB-001, DELIVERY_PLAN §5.5).

Jobs are PostgreSQL-backed so local and cloud behavior are identical and no
essential state lives only in a provider queue. This module defines the
schema and the legal state transitions; enqueueing is JOB-002 and claiming
(``FOR UPDATE SKIP LOCKED``) is JOB-003.

The jobs table is deliberately NOT under row-level security: the worker is
a cross-tenant system actor (like the outbox publisher) and binds no tenant
GUC. Tenant scoping for user-facing job reads is enforced at the API layer
(JOB-006).

States::

    pending ──▶ running ──▶ succeeded
       │           │
       │           ├──▶ pending      (retryable failure / lock recovery)
       │           ├──▶ dead_letter  (permanent failure or attempts exhausted)
       │           └──▶ cancelled
       └──▶ cancelled
    dead_letter ──▶ pending          (audited replay, JOB-006)

Every other transition raises ``InvalidJobTransition`` — including
resurrecting a succeeded or cancelled job.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, case, event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.base import NO_VALUE

from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.types import GUID, UTCDateTime, utcnow


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class FailureClass(StrEnum):
    """How a handler classifies a failure (JOB-005).

    TRANSIENT (network blip, provider timeout, lock contention) retries with
    bounded exponential backoff. PERMANENT (invalid input, security
    violation, unprocessable document) never retries — repeating the attempt
    can only repeat the outcome, and security failures must not be hammered.
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.CANCELLED})
"""States a job can never leave. dead_letter is inspectable and replayable."""

_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (JobStatus.PENDING, JobStatus.RUNNING),
        (JobStatus.PENDING, JobStatus.CANCELLED),
        (JobStatus.RUNNING, JobStatus.SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.PENDING),
        (JobStatus.RUNNING, JobStatus.DEAD_LETTER),
        (JobStatus.RUNNING, JobStatus.CANCELLED),
        (JobStatus.DEAD_LETTER, JobStatus.PENDING),
    }
)


class InvalidJobTransition(Exception):
    def __init__(self, *, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"illegal job transition: {current!r} -> {requested!r}")


class JobLockError(Exception):
    """Raised when a worker touches a job whose lock it does not hold."""

    def __init__(self, *, job_id: uuid.UUID, worker_id: str, reason: str) -> None:
        self.job_id = job_id
        self.worker_id = worker_id
        super().__init__(f"job {job_id}: {reason} (worker {worker_id!r})")


class Job(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "jobs"

    def __init__(self, **kwargs: Any) -> None:
        # Column defaults apply at INSERT; the state machine needs a concrete
        # starting state the moment the object exists.
        kwargs.setdefault("status", JobStatus.PENDING)
        super().__init__(**kwargs)

    job_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    # Recorded so consumers can migrate old payloads after a shape change.
    payload_schema_version: Mapped[int] = mapped_column(nullable=False, default=1)

    # Nullable: system jobs (outbox drain, cleanup) have no tenant.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JobStatus.PENDING)
    # Lower value claims first among due jobs; ties broken by run_after.
    priority: Mapped[int] = mapped_column(nullable=False, default=100)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=5)
    dedupe_key: Mapped[str | None] = mapped_column(String(300), nullable=True, unique=True)

    lock_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lock_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    # Safe summary only — never raw payloads, stack traces with secrets, etc.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'dead_letter', 'cancelled')",
            name="jobs_status_valid",
        ),
        CheckConstraint("attempts >= 0", name="jobs_attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="jobs_max_attempts_positive"),
        Index("ix_jobs_claim", "status", "priority", "run_after"),
    )


@event.listens_for(Job.status, "set")
def _guard_status_transition(target: Job, value: str, oldvalue: object, initiator: object) -> None:
    """Enforce the state machine on every in-Python status assignment.

    ORM loading populates attributes without firing ``set``, so this guards
    application code only; the CHECK constraint guards the value set at the
    database level.
    """
    if oldvalue is NO_VALUE or oldvalue is None:
        # Newly constructed jobs must start pending — scheduled/delayed jobs
        # express the delay through run_after, not a special state.
        if value != JobStatus.PENDING:
            raise InvalidJobTransition(current="<new>", requested=str(value))
        return
    if oldvalue == value:
        return
    if (str(oldvalue), str(value)) not in _ALLOWED_TRANSITIONS:
        raise InvalidJobTransition(current=str(oldvalue), requested=str(value))


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: str,
    payload: dict[str, Any],
    organization_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    correlation_id: str | None = None,
    priority: int = 100,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    payload_schema_version: int = 1,
) -> Job:
    """Stage a job in the caller's transaction (JOB-002).

    Enqueued in the SAME session as the domain change, so a commit persists
    both and a rollback discards both — committed changes never lose their
    follow-up job and rolled-back changes create none.

    With a ``dedupe_key``, a second enqueue returns the existing job (in any
    state) instead of creating a duplicate; intents that must legitimately
    re-run encode the stage/run in the key. The unique constraint backs this
    up against concurrent writers — an IntegrityError there means another
    transaction already enqueued the same intent.

    ``run_after`` in the future makes a scheduled job: it stays pending and
    is not claimable until then (JOB-003).
    """
    if dedupe_key is not None:
        existing = (
            await session.execute(select(Job).where(Job.dedupe_key == dedupe_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    job = Job(
        job_type=job_type,
        payload=payload,
        organization_id=organization_id,
        dedupe_key=dedupe_key,
        correlation_id=correlation_id,
        priority=priority,
        max_attempts=max_attempts,
        payload_schema_version=payload_schema_version,
        **({"run_after": run_after} if run_after is not None else {}),
    )
    session.add(job)
    return job


#: Jobs overdue by more than this jump the priority queue, bounding how long
#: a low-priority job can be starved by a stream of higher-priority work.
STARVATION_THRESHOLD = timedelta(minutes=15)


async def claim_next_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 1,
    lock_duration: timedelta = timedelta(minutes=5),
    job_types: Sequence[str] | None = None,
    starvation_threshold: timedelta = STARVATION_THRESHOLD,
    now: datetime | None = None,
) -> list[Job]:
    """Claim up to ``limit`` due jobs for ``worker_id`` (JOB-003).

    Uses ``FOR UPDATE SKIP LOCKED`` on PostgreSQL so concurrent workers never
    select the same row: a job is either locked by this transaction or
    invisible to it. Claimed jobs move to ``running`` with lock ownership,
    expiration, heartbeat, and an incremented attempt count — all inside the
    caller's transaction, so a crash before commit leaves the job pending.

    Ordering: overdue-beyond-threshold jobs first (starvation bound), then
    priority (lower value first), then run_after (oldest first).

    ``limit`` is the worker's concurrency budget for this poll; callers pass
    the number of free execution slots.
    """
    current = now or utcnow()
    starved = case((Job.run_after < current - starvation_threshold, 0), else_=1)
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.PENDING, Job.run_after <= current)
        .order_by(starved, Job.priority, Job.run_after)
        .limit(limit)
    )
    if job_types is not None:
        stmt = stmt.where(Job.job_type.in_(job_types))
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    jobs = list((await session.execute(stmt)).scalars().all())
    for job in jobs:
        job.status = JobStatus.RUNNING
        job.lock_owner = worker_id
        job.lock_expires_at = current + lock_duration
        job.heartbeat_at = current
        job.attempts += 1
    await session.flush()
    return jobs


def heartbeat(
    job: Job,
    *,
    worker_id: str,
    lock_duration: timedelta = timedelta(minutes=5),
    now: datetime | None = None,
) -> None:
    """Extend the lock for a long-running execution (JOB-004).

    Only the lock owner may extend; anything else is a bug or an expired
    claim that recovery already handed to someone else.
    """
    current = now or utcnow()
    if job.status != JobStatus.RUNNING:
        raise JobLockError(
            job_id=job.id, worker_id=worker_id, reason=f"cannot heartbeat a {job.status} job"
        )
    if job.lock_owner != worker_id:
        raise JobLockError(
            job_id=job.id,
            worker_id=worker_id,
            reason=f"lock is held by {job.lock_owner!r}",
        )
    job.heartbeat_at = current
    job.lock_expires_at = current + lock_duration


def return_to_queue(job: Job, *, worker_id: str) -> None:
    """Graceful cancellation: a terminating worker hands its claim back
    (JOB-004). The attempt is refunded — an interrupted run is not a
    failure, and repeated deploys must not dead-letter healthy jobs.
    """
    if job.status != JobStatus.RUNNING or job.lock_owner != worker_id:
        raise JobLockError(
            job_id=job.id,
            worker_id=worker_id,
            reason=f"cannot return a job in state {job.status} owned by {job.lock_owner!r}",
        )
    job.status = JobStatus.PENDING
    job.lock_owner = None
    job.lock_expires_at = None
    job.heartbeat_at = None
    job.attempts = max(0, job.attempts - 1)


def _require_lock(job: Job, worker_id: str, action: str) -> None:
    if job.status != JobStatus.RUNNING or job.lock_owner != worker_id:
        raise JobLockError(
            job_id=job.id,
            worker_id=worker_id,
            reason=f"cannot {action} a job in state {job.status} owned by {job.lock_owner!r}",
        )


#: Backoff for transient failures: 10s, 20s, 40s, ... capped at 10 minutes.
RETRY_BACKOFF_BASE = timedelta(seconds=10)
RETRY_BACKOFF_CAP = timedelta(minutes=10)


def retry_backoff(attempts: int) -> timedelta:
    """Bounded exponential backoff for the given completed attempt count."""
    # Cap the exponent first: 2**10 already exceeds the cap, and huge
    # attempt counts must not overflow timedelta arithmetic.
    exponent = min(max(0, attempts - 1), 10)
    scaled = RETRY_BACKOFF_BASE * int(2**exponent)
    return min(scaled, RETRY_BACKOFF_CAP)


def mark_succeeded(job: Job, *, worker_id: str, now: datetime | None = None) -> None:
    """Complete a job (JOB-005). Only the lock owner may complete it."""
    current = now or utcnow()
    _require_lock(job, worker_id, "complete")
    job.status = JobStatus.SUCCEEDED
    job.lock_owner = None
    job.lock_expires_at = None
    job.heartbeat_at = None
    job.finished_at = current
    job.last_error = None


def mark_failed(
    job: Job,
    *,
    worker_id: str,
    error: str,
    failure_class: FailureClass,
    retry_in: timedelta | None = None,
    now: datetime | None = None,
) -> None:
    """Record a failed execution (JOB-005).

    PERMANENT failures dead-letter immediately regardless of remaining
    attempts. TRANSIENT failures reschedule with bounded exponential
    backoff — or the handler's explicit ``retry_in`` hint (a provider's
    Retry-After, for example) — until attempts are exhausted, then
    dead-letter. ``attempts`` was counted at claim time and is preserved
    through every transition, so the history stays visible.

    ``error`` must be a SAFE summary: no payload contents, no secrets.
    """
    current = now or utcnow()
    _require_lock(job, worker_id, "fail")
    job.lock_owner = None
    job.lock_expires_at = None
    job.heartbeat_at = None
    job.last_error = error[:500]
    if failure_class is FailureClass.PERMANENT or job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD_LETTER
        job.finished_at = current
        return
    delay = retry_in if retry_in is not None else retry_backoff(job.attempts)
    job.status = JobStatus.PENDING
    job.run_after = current + delay


async def recover_expired_locks(
    session: AsyncSession,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> list[Job]:
    """Return crashed workers' jobs to the queue (JOB-004).

    Only RUNNING jobs with an expired lock are touched — succeeded,
    cancelled, and dead-letter jobs are never resurrected, whatever their
    lock columns say. A job that expired on its final permitted attempt
    goes to dead_letter instead of looping forever.
    """
    current = now or utcnow()
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.lock_expires_at < current)
        .order_by(Job.lock_expires_at)
        .limit(limit)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    expired = list((await session.execute(stmt)).scalars().all())
    for job in expired:
        previous_owner = job.lock_owner
        job.lock_owner = None
        job.lock_expires_at = None
        job.heartbeat_at = None
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.DEAD_LETTER
            job.finished_at = current
            job.last_error = (
                f"lock held by {previous_owner!r} expired on final attempt "
                f"{job.attempts}/{job.max_attempts}"
            )[:500]
        else:
            job.status = JobStatus.PENDING
    await session.flush()
    return expired
