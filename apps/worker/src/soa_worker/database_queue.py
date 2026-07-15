"""PostgreSQL-backed adapter between the generic worker loop and JOB-001.

Claim, acknowledgement, retry, lock recovery, and heartbeat all use short
transactions. A handler receives only the already-committed claim; its
domain transaction completes before the queue acknowledgement, so a crash
between the two is safe: lock recovery redelivers an idempotent handler.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import DatabaseSessions
from soa_db.jobs import (
    FailureClass,
    Job,
    JobLockError,
    JobStatus,
    claim_next_jobs,
    heartbeat,
    mark_failed,
    mark_succeeded,
    recover_expired_locks,
)
from soa_worker.job_metrics import JobMetrics
from soa_worker.registry import JobEnvelope
from soa_worker.worker import JobFailureResult

logger = logging.getLogger(__name__)

ReconcileTerminalFailures = Callable[[], Awaitable[int]]


class DatabaseJobQueue:
    def __init__(
        self,
        db: DatabaseSessions,
        *,
        worker_id: str,
        reconcile_terminal_failures: ReconcileTerminalFailures | None = None,
        reconciliation_interval_seconds: float = 30.0,
        metrics: JobMetrics | None = None,
        observation_interval_seconds: float = 15.0,
        lock_recovery_interval_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(
            interval < 0
            for interval in (
                reconciliation_interval_seconds,
                observation_interval_seconds,
                lock_recovery_interval_seconds,
            )
        ):
            raise ValueError("queue maintenance intervals cannot be negative")
        self._db = db
        self._worker_id = worker_id
        self._reconcile_terminal_failures = reconcile_terminal_failures
        self._reconciliation_interval_seconds = reconciliation_interval_seconds
        self._metrics = metrics
        self._observation_interval_seconds = observation_interval_seconds
        self._lock_recovery_interval_seconds = lock_recovery_interval_seconds
        self._monotonic = monotonic
        self._next_reconciliation_at = 0.0
        self._next_observation_at = 0.0
        self._next_lock_recovery_at = 0.0

    async def claim(self) -> JobEnvelope | None:
        envelope: JobEnvelope | None = None
        recovered: list[Job] = []
        claimed: list[Job] = []
        async with self._db.session_scope() as session:
            current = self._monotonic()
            if current >= self._next_lock_recovery_at:
                self._next_lock_recovery_at = current + self._lock_recovery_interval_seconds
                recovered = await recover_expired_locks(session)
            claimed = await claim_next_jobs(
                session,
                worker_id=self._worker_id,
                limit=1,
            )
            if claimed:
                job = claimed[0]
                envelope = JobEnvelope(
                    job_id=job.id,
                    job_type=job.job_type,
                    payload=dict(job.payload),
                    correlation_id=job.correlation_id,
                    organization_id=job.organization_id,
                    lease_attempt=job.attempts,
                )
        if self._metrics is not None:
            self._metrics.record_claimed(claimed)
            for job in recovered:
                if job.status == JobStatus.DEAD_LETTER:
                    self._metrics.record_dead_lettered(job)
                else:
                    self._metrics.record_retried(job)
            await self._observe_if_due()
        # Lock recovery may itself dead-letter a final attempt. Reconcile only
        # after that queue transaction commits, preserving the independent
        # domain-state write boundary.
        await self._reconcile_if_due()
        return envelope

    async def succeeded(self, envelope: JobEnvelope) -> None:
        completed: Job | None = None
        async with self._db.session_scope() as session:
            record = await self._current_lease(session, envelope)
            if record is not None:
                mark_succeeded(record, worker_id=self._worker_id)
                completed = record
        if completed is not None and self._metrics is not None:
            self._metrics.record_completed(completed)

    async def failed(self, envelope: JobEnvelope, error: BaseException) -> JobFailureResult | None:
        safe_error = f"{envelope.job_type} handler failed ({type(error).__name__})"
        failed: Job | None = None
        async with self._db.session_scope() as session:
            record = await self._current_lease(session, envelope)
            if record is None:
                return None
            failure_class = (
                FailureClass.PERMANENT
                if isinstance(error, (KeyError, ValueError))
                else FailureClass.TRANSIENT
            )
            # Exception type is useful operationally and cannot contain a
            # payload/secret. Handler messages are deliberately not copied.
            mark_failed(
                record,
                worker_id=self._worker_id,
                error=safe_error,
                failure_class=failure_class,
            )
            terminal = record.status == JobStatus.DEAD_LETTER
            failed = record
        if failed is not None and self._metrics is not None:
            if terminal:
                self._metrics.record_dead_lettered(failed)
            else:
                self._metrics.record_retried(failed)
        return JobFailureResult(terminal=terminal, safe_error=safe_error)

    async def heartbeat(self, envelope: JobEnvelope) -> None:
        async with self._db.session_scope() as session:
            record = await self._current_lease(session, envelope)
            if record is not None:
                heartbeat(record, worker_id=self._worker_id)

    async def _current_lease(self, session: AsyncSession, envelope: JobEnvelope) -> Job | None:
        if envelope.job_id is None:
            raise ValueError("database queue envelopes require a job id")
        if envelope.lease_attempt is None:
            raise ValueError("database queue envelopes require a lease attempt")
        statement = select(Job).where(Job.id == envelope.job_id).with_for_update()
        record = (await session.execute(statement)).scalar_one_or_none()
        if record is None:
            return None
        if (
            record.status != JobStatus.RUNNING
            or record.lock_owner != self._worker_id
            or record.attempts != envelope.lease_attempt
        ):
            raise JobLockError(
                job_id=record.id,
                worker_id=self._worker_id,
                reason=(
                    "stale lease: expected running attempt "
                    f"{envelope.lease_attempt}, found {record.status} attempt "
                    f"{record.attempts} owned by {record.lock_owner!r}"
                ),
            )
        return record

    async def _reconcile_if_due(self) -> None:
        reconcile = self._reconcile_terminal_failures
        if reconcile is None:
            return
        current = self._monotonic()
        if current < self._next_reconciliation_at:
            return
        self._next_reconciliation_at = current + self._reconciliation_interval_seconds
        try:
            await reconcile()
        except Exception:
            # Claiming healthy work must continue if the independent domain
            # reconciliation transaction is temporarily unavailable.
            logger.exception("terminal domain failure reconciliation failed")

    async def _observe_if_due(self) -> None:
        metrics = self._metrics
        if metrics is None:
            return
        current = self._monotonic()
        if current < self._next_observation_at:
            return
        self._next_observation_at = current + self._observation_interval_seconds
        try:
            async with self._db.session_scope() as session:
                await metrics.observe_queue(session)
        except Exception:
            # Metrics must never stop the durable claim path. The next
            # scheduled observation retries without changing queue state.
            logger.exception("queue metrics observation failed")


__all__ = ["DatabaseJobQueue"]
