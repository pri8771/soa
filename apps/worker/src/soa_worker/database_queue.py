"""PostgreSQL-backed adapter between the generic worker loop and JOB-001.

Claim, acknowledgement, retry, lock recovery, and heartbeat all use short
transactions. A handler receives only the already-committed claim; its
domain transaction completes before the queue acknowledgement, so a crash
between the two is safe: lock recovery redelivers an idempotent handler.
"""

import uuid

from soa_db import DatabaseSessions
from soa_db.jobs import (
    FailureClass,
    Job,
    claim_next_jobs,
    heartbeat,
    mark_failed,
    mark_succeeded,
    recover_expired_locks,
)
from soa_worker.registry import JobEnvelope


class DatabaseJobQueue:
    def __init__(self, db: DatabaseSessions, *, worker_id: str) -> None:
        self._db = db
        self._worker_id = worker_id

    async def claim(self) -> JobEnvelope | None:
        async with self._db.session_scope() as session:
            await recover_expired_locks(session)
            jobs = await claim_next_jobs(
                session,
                worker_id=self._worker_id,
                limit=1,
            )
            if not jobs:
                return None
            job = jobs[0]
            return JobEnvelope(
                job_id=job.id,
                job_type=job.job_type,
                payload=dict(job.payload),
                correlation_id=job.correlation_id,
            )

    async def succeeded(self, envelope: JobEnvelope) -> None:
        job = await self._locked_job(envelope)
        async with self._db.session_scope() as session:
            record = await session.get(Job, job)
            if record is not None:
                mark_succeeded(record, worker_id=self._worker_id)

    async def failed(self, envelope: JobEnvelope, error: BaseException) -> None:
        job = await self._locked_job(envelope)
        async with self._db.session_scope() as session:
            record = await session.get(Job, job)
            if record is None:
                return
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
                error=f"{envelope.job_type} handler failed ({type(error).__name__})",
                failure_class=failure_class,
            )

    async def heartbeat(self, envelope: JobEnvelope) -> None:
        job = await self._locked_job(envelope)
        async with self._db.session_scope() as session:
            record = await session.get(Job, job)
            if record is not None:
                heartbeat(record, worker_id=self._worker_id)

    async def _locked_job(self, envelope: JobEnvelope) -> uuid.UUID:
        if envelope.job_id is None:
            raise ValueError("database queue envelopes require a job id")
        return envelope.job_id


__all__ = ["DatabaseJobQueue"]
