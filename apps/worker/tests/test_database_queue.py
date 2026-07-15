"""The production worker actually claims and acknowledges durable jobs."""

from pathlib import Path

from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.jobs import Job, JobStatus, enqueue_job
from soa_worker.database_queue import DatabaseJobQueue
from soa_worker.registry import HandlerRegistry, JobEnvelope


async def make_queue(tmp_path: Path) -> tuple[DatabaseSessions, DatabaseJobQueue]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/queue.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    registry = HandlerRegistry()

    @registry.register("known")
    async def known(_job: JobEnvelope) -> None:
        return None

    return db, DatabaseJobQueue(db, worker_id="worker:test")


async def load_job(db: DatabaseSessions) -> Job:
    async with db.session_scope() as session:
        return (await session.execute(select(Job))).scalar_one()


async def test_claim_and_success_are_durable(tmp_path: Path) -> None:
    db, queue = await make_queue(tmp_path)
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={"safe": "value"})

    envelope = await queue.claim()
    assert envelope is not None
    assert envelope.job_type == "known"
    assert (await load_job(db)).status == JobStatus.RUNNING

    await queue.succeeded(envelope)
    assert (await load_job(db)).status == JobStatus.SUCCEEDED
    await db.dispose()


async def test_transient_handler_failure_returns_job_to_queue(tmp_path: Path) -> None:
    db, queue = await make_queue(tmp_path)
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={})

    envelope = await queue.claim()
    assert envelope is not None
    await queue.failed(envelope, RuntimeError("customer payload must not be copied"))
    record = await load_job(db)
    assert record.status == JobStatus.PENDING
    assert record.last_error == "known handler failed (RuntimeError)"
    await db.dispose()


async def test_invalid_payload_failure_dead_letters_without_message_leak(tmp_path: Path) -> None:
    db, queue = await make_queue(tmp_path)
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={})

    envelope = await queue.claim()
    assert envelope is not None
    await queue.failed(envelope, ValueError("CANARY-secret-document-value"))
    record = await load_job(db)
    assert record.status == JobStatus.DEAD_LETTER
    assert "CANARY" not in (record.last_error or "")
    await db.dispose()
