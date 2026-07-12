"""Concurrent job-claim tests against real PostgreSQL (JOB-003).

``FOR UPDATE SKIP LOCKED`` only exists on PostgreSQL, so the core guarantee
— two workers polling at the same moment never claim the same job — must be
proven here, not on SQLite. CI runs this in the migrations job as the
non-superuser application role.
"""

import os
import uuid

import pytest
from sqlalchemy import delete

from soa_db import DatabaseSessions, create_database_engine
from soa_db.jobs import Job, JobStatus, claim_next_jobs, enqueue_job

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; SKIP LOCKED requires real PostgreSQL",
    ),
]


@pytest.fixture
async def db() -> DatabaseSessions:
    assert POSTGRES_URL is not None
    sessions = DatabaseSessions(create_database_engine(POSTGRES_URL))
    yield sessions
    async with sessions.session_scope() as session:
        await session.execute(delete(Job).where(Job.job_type.like("claimtest.%")))
    await sessions.dispose()


async def test_overlapping_transactions_never_claim_the_same_job(db: DatabaseSessions) -> None:
    job_type = f"claimtest.{uuid.uuid4().hex[:8]}"
    async with db.session_scope() as session:
        for _ in range(5):
            await enqueue_job(session, job_type=job_type, payload={})

    # Two workers poll while neither has committed: SKIP LOCKED must hand
    # each a disjoint set, with no blocking and no double execution.
    async with db.session_scope() as first_worker:
        first = await claim_next_jobs(
            first_worker, worker_id="worker-1", limit=3, job_types=[job_type]
        )
        async with db.session_scope() as second_worker:
            second = await claim_next_jobs(
                second_worker, worker_id="worker-2", limit=3, job_types=[job_type]
            )
            first_ids = {job.id for job in first}
            second_ids = {job.id for job in second}
            assert len(first) == 3
            assert len(second) == 2, "second worker must see only unclaimed jobs"
            assert first_ids & second_ids == set()

    async with db.session_scope() as session:
        from sqlalchemy import select

        stored = (
            (await session.execute(select(Job).where(Job.job_type == job_type))).scalars().all()
        )
        assert all(job.status == JobStatus.RUNNING for job in stored)
        owners = {job.lock_owner for job in stored}
        assert owners == {"worker-1", "worker-2"}
