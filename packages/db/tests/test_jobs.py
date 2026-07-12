"""Job schema, state-machine, and transactional-enqueue tests (JOB-001/002)."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, DatabaseSessions, create_database_engine, utcnow
from soa_db.jobs import InvalidJobTransition, Job, JobStatus, claim_next_jobs, enqueue_job
from soa_db.mixins import UuidPrimaryKeyMixin


class Document(UuidPrimaryKeyMixin, Base):
    """Stand-in domain entity for transaction-boundary tests."""

    __tablename__ = "test_document"
    title: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/jobs.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def make_job(**overrides: object) -> Job:
    values: dict[str, object] = {
        "job_type": "document.extract",
        "payload": {"document_id": "doc-1"},
    }
    values.update(overrides)
    return Job(**values)


async def test_defaults_record_schema_version_and_scheduling_fields(
    sessions: DatabaseSessions,
) -> None:
    job = make_job()
    async with sessions.session_scope() as session:
        session.add(job)
    async with sessions.session_scope() as session:
        stored = (await session.execute(select(Job))).scalar_one()
        assert stored.status == JobStatus.PENDING
        assert stored.payload_schema_version == 1
        assert stored.attempts == 0
        assert stored.max_attempts == 5
        assert stored.priority == 100
        assert stored.run_after is not None
        assert stored.payload == {"document_id": "doc-1"}


async def test_happy_path_lifecycle_persists(sessions: DatabaseSessions) -> None:
    job = make_job()
    async with sessions.session_scope() as session:
        session.add(job)
    async with sessions.session_scope() as session:
        stored = (await session.execute(select(Job))).scalar_one()
        stored.status = JobStatus.RUNNING
        stored.status = JobStatus.SUCCEEDED
    async with sessions.session_scope() as session:
        stored = (await session.execute(select(Job))).scalar_one()
        assert stored.status == JobStatus.SUCCEEDED


async def test_retry_and_replay_transitions_are_legal() -> None:
    job = make_job()
    job.status = JobStatus.RUNNING
    job.status = JobStatus.PENDING  # retryable failure returns to queue
    job.status = JobStatus.RUNNING
    job.status = JobStatus.DEAD_LETTER  # attempts exhausted
    job.status = JobStatus.PENDING  # audited replay (JOB-006)
    assert job.status == JobStatus.PENDING


@pytest.mark.parametrize(
    ("path", "illegal_next"),
    [
        # A job cannot succeed without ever running.
        ([], JobStatus.SUCCEEDED),
        ([], JobStatus.DEAD_LETTER),
        # succeeded is terminal — no resurrection.
        ([JobStatus.RUNNING, JobStatus.SUCCEEDED], JobStatus.PENDING),
        ([JobStatus.RUNNING, JobStatus.SUCCEEDED], JobStatus.RUNNING),
        # cancelled is terminal.
        ([JobStatus.CANCELLED], JobStatus.PENDING),
        ([JobStatus.CANCELLED], JobStatus.RUNNING),
        # dead-letter jobs replay through pending, never straight to running.
        ([JobStatus.RUNNING, JobStatus.DEAD_LETTER], JobStatus.RUNNING),
        ([JobStatus.RUNNING, JobStatus.DEAD_LETTER], JobStatus.SUCCEEDED),
    ],
)
async def test_illegal_transitions_raise(path: list[JobStatus], illegal_next: JobStatus) -> None:
    job = make_job()
    for status in path:
        job.status = status
    with pytest.raises(InvalidJobTransition):
        job.status = illegal_next


async def test_new_jobs_must_start_pending() -> None:
    with pytest.raises(InvalidJobTransition):
        make_job(status=JobStatus.RUNNING)


async def test_reassigning_the_same_status_is_a_noop() -> None:
    job = make_job()
    job.status = JobStatus.PENDING
    assert job.status == JobStatus.PENDING


async def test_dedupe_key_is_unique(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        session.add(make_job(dedupe_key="extract:doc-1"))
    with pytest.raises(IntegrityError):
        async with sessions.session_scope() as session:
            session.add(make_job(dedupe_key="extract:doc-1"))


async def test_database_rejects_unknown_status_values(sessions: DatabaseSessions) -> None:
    """The CHECK constraint backs up the Python guard at the SQL layer."""
    async with sessions.session_scope() as session:
        session.add(make_job())
    with pytest.raises(IntegrityError):
        async with sessions.session_scope() as session:
            await session.execute(update(Job).values(status="exploded"))


async def test_domain_change_and_job_commit_atomically(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        session.add(Document(title="PO-1001"))
        await enqueue_job(
            session,
            job_type="document.extract",
            payload={"title": "PO-1001"},
            correlation_id="corr-1",
        )
    async with sessions.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
    assert len(documents) == 1
    assert len(jobs) == 1
    assert jobs[0].correlation_id == "corr-1"


async def test_rolled_back_domain_change_creates_no_job(sessions: DatabaseSessions) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with sessions.session_scope() as session:
            session.add(Document(title="PO-1002"))
            await enqueue_job(session, job_type="document.extract", payload={})
            raise RuntimeError("boom")
    async with sessions.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
    assert documents == []
    assert jobs == []


async def test_enqueue_with_dedupe_key_absorbs_duplicates(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        first = await enqueue_job(
            session, job_type="document.extract", payload={}, dedupe_key="extract:doc-9"
        )
        await session.flush()
        first_id = first.id
    async with sessions.session_scope() as session:
        second = await enqueue_job(
            session, job_type="document.extract", payload={}, dedupe_key="extract:doc-9"
        )
        assert second.id == first_id
    async with sessions.session_scope() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
    assert len(jobs) == 1


async def test_scheduled_job_records_future_run_after(sessions: DatabaseSessions) -> None:
    later = utcnow() + timedelta(hours=2)
    async with sessions.session_scope() as session:
        await enqueue_job(
            session, job_type="report.nightly", payload={}, run_after=later, priority=200
        )
    async with sessions.session_scope() as session:
        stored = (await session.execute(select(Job))).scalar_one()
        assert stored.run_after == later
        assert stored.priority == 200
        assert stored.status == JobStatus.PENDING


# ---------------------------------------------------------------------------
# Claiming (JOB-003). SQLite covers ordering/locking bookkeeping; the
# SKIP LOCKED concurrency guarantee itself is tested against real
# PostgreSQL in tests/jobs/test_claim_postgres.py.
# ---------------------------------------------------------------------------


async def test_claim_orders_by_priority_then_age_and_respects_limit(
    sessions: DatabaseSessions,
) -> None:
    base = utcnow() - timedelta(minutes=1)
    async with sessions.session_scope() as session:
        await enqueue_job(session, job_type="low", payload={}, priority=200, run_after=base)
        await enqueue_job(
            session,
            job_type="high-old",
            payload={},
            priority=50,
            run_after=base - timedelta(seconds=30),
        )
        await enqueue_job(session, job_type="high-new", payload={}, priority=50, run_after=base)
    async with sessions.session_scope() as session:
        claimed = await claim_next_jobs(session, worker_id="w-1", limit=2)
        assert [job.job_type for job in claimed] == ["high-old", "high-new"]


async def test_claim_marks_lock_ownership_and_attempts(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        await enqueue_job(session, job_type="work", payload={})
    moment = utcnow()
    async with sessions.session_scope() as session:
        claimed = await claim_next_jobs(
            session, worker_id="worker-a", lock_duration=timedelta(minutes=10), now=moment
        )
        (job,) = claimed
        assert job.status == JobStatus.RUNNING
        assert job.lock_owner == "worker-a"
        assert job.lock_expires_at == moment + timedelta(minutes=10)
        assert job.heartbeat_at == moment
        assert job.attempts == 1


async def test_claim_skips_running_scheduled_and_foreign_type_jobs(
    sessions: DatabaseSessions,
) -> None:
    async with sessions.session_scope() as session:
        await enqueue_job(session, job_type="due", payload={})
        await enqueue_job(
            session, job_type="future", payload={}, run_after=utcnow() + timedelta(hours=1)
        )
        await enqueue_job(session, job_type="other", payload={})
    async with sessions.session_scope() as session:
        first = await claim_next_jobs(session, worker_id="w-1", limit=10, job_types=["due"])
        assert [job.job_type for job in first] == ["due"]
    async with sessions.session_scope() as session:
        second = await claim_next_jobs(session, worker_id="w-2", limit=10)
        # The running "due" job and the future job are not claimable.
        assert [job.job_type for job in second] == ["other"]


async def test_starved_low_priority_jobs_jump_the_queue(sessions: DatabaseSessions) -> None:
    now = utcnow()
    async with sessions.session_scope() as session:
        await enqueue_job(
            session,
            job_type="starved-low",
            payload={},
            priority=900,
            run_after=now - timedelta(hours=1),
        )
        await enqueue_job(session, job_type="fresh-high", payload={}, priority=1, run_after=now)
    async with sessions.session_scope() as session:
        claimed = await claim_next_jobs(session, worker_id="w-1", limit=1, now=now)
        assert [job.job_type for job in claimed] == ["starved-low"]
