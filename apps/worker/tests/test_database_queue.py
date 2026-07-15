"""The production worker actually claims and acknowledges durable jobs."""

import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import select

import soa_worker.database_queue as database_queue_module
from soa_config.telemetry import configure_telemetry
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.jobs import (
    Job,
    JobLockError,
    JobStatus,
    claim_next_jobs,
    enqueue_job,
    recover_expired_locks,
)
from soa_db.types import utcnow
from soa_worker.database_queue import DatabaseJobQueue
from soa_worker.job_metrics import JobMetrics
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
    organization_id = uuid.uuid4()
    async with db.session_scope() as session:
        await enqueue_job(
            session,
            job_type="known",
            payload={"safe": "value"},
            organization_id=organization_id,
        )

    envelope = await queue.claim()
    assert envelope is not None
    assert envelope.job_type == "known"
    assert envelope.organization_id == organization_id
    assert envelope.lease_attempt == 1
    assert (await load_job(db)).status == JobStatus.RUNNING

    await queue.succeeded(envelope)
    assert (await load_job(db)).status == JobStatus.SUCCEEDED
    await db.dispose()


async def test_stale_handler_cannot_ack_a_reclaimed_same_worker_lease(tmp_path: Path) -> None:
    db, queue = await make_queue(tmp_path)
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={})

    stale = await queue.claim()
    assert stale is not None and stale.lease_attempt == 1
    reclaimed_at = utcnow() + timedelta(minutes=6)
    async with db.session_scope() as session:
        recovered = await recover_expired_locks(session, now=reclaimed_at)
        assert len(recovered) == 1
        reclaimed = await claim_next_jobs(
            session,
            worker_id="worker:test",
            now=reclaimed_at,
        )
        assert len(reclaimed) == 1 and reclaimed[0].attempts == 2

    with pytest.raises(JobLockError, match="stale lease"):
        await queue.succeeded(stale)
    record = await load_job(db)
    assert record.status == JobStatus.RUNNING
    assert record.attempts == 2
    assert record.lock_owner == "worker:test"
    await db.dispose()


async def test_transient_handler_failure_returns_job_to_queue(tmp_path: Path) -> None:
    db, queue = await make_queue(tmp_path)
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={})

    envelope = await queue.claim()
    assert envelope is not None
    result = await queue.failed(envelope, RuntimeError("customer payload must not be copied"))
    assert result is not None and result.terminal is False
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
    result = await queue.failed(envelope, ValueError("CANARY-secret-document-value"))
    assert result is not None and result.terminal is True
    assert "CANARY" not in result.safe_error
    record = await load_job(db)
    assert record.status == JobStatus.DEAD_LETTER
    assert "CANARY" not in (record.last_error or "")
    await db.dispose()


async def test_transient_failure_on_exhausted_attempt_is_terminal(tmp_path: Path) -> None:
    db, queue = await make_queue(tmp_path)
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={}, max_attempts=1)

    envelope = await queue.claim()
    assert envelope is not None
    result = await queue.failed(envelope, RuntimeError("temporary provider failure"))
    assert result is not None and result.terminal is True
    assert (await load_job(db)).status == JobStatus.DEAD_LETTER
    await db.dispose()


async def test_claim_runs_bounded_terminal_reconciliation_on_schedule(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/reconcile-queue.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    calls = 0

    async def reconcile() -> int:
        nonlocal calls
        calls += 1
        return 0

    now = 100.0
    queue = DatabaseJobQueue(
        db,
        worker_id="worker:test",
        reconcile_terminal_failures=reconcile,
        reconciliation_interval_seconds=30,
        monotonic=lambda: now,
    )
    assert await queue.claim() is None
    assert await queue.claim() is None
    assert calls == 1
    now = 131.0
    assert await queue.claim() is None
    assert calls == 2
    await db.dispose()


async def test_lock_recovery_scan_is_bounded_instead_of_running_on_every_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/bounded-recovery.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    recover = AsyncMock(return_value=[])
    monkeypatch.setattr(database_queue_module, "recover_expired_locks", recover)
    now = 100.0
    queue = DatabaseJobQueue(
        db,
        worker_id="worker:test",
        lock_recovery_interval_seconds=15,
        monotonic=lambda: now,
    )

    assert await queue.claim() is None
    assert await queue.claim() is None
    assert recover.await_count == 1
    now = 116.0
    assert await queue.claim() is None
    assert recover.await_count == 2
    await db.dispose()


async def test_queue_adapter_emits_claim_and_completion_metrics(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/queue-metrics.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    reader = InMemoryMetricReader()
    telemetry = configure_telemetry(
        service_name="soa-worker-test",
        environment="test",
        profile="console",
        metric_reader=reader,
    )
    queue = DatabaseJobQueue(
        db,
        worker_id="worker:test",
        metrics=JobMetrics(telemetry),
    )
    async with db.session_scope() as session:
        await enqueue_job(session, job_type="known", payload={})

    envelope = await queue.claim()
    assert envelope is not None
    await queue.succeeded(envelope)

    data = reader.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert {
        "soa.jobs.claimed",
        "soa.jobs.completed",
        "soa.jobs.queue_depth",
        "soa.jobs.oldest_pending_age_seconds",
    } <= names
    await db.dispose()
