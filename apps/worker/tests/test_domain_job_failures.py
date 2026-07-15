import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.data_export_jobs import (
    DataExportJobRepository,
    DataExportState,
    new_data_export_job,
)
from soa_db.deletion_requests import (
    DOCUMENT_DELETION_JOB_TYPE,
    DeletionRequest,
    DeletionRequestRepository,
    DeletionRequestState,
)
from soa_db.evaluation_runs import (
    EvaluationRunRepository,
    EvaluationRunState,
    create_evaluation_run,
)
from soa_db.jobs import Job, JobStatus, enqueue_job
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow
from soa_worker.database_queue import DatabaseJobQueue
from soa_worker.domain_job_failures import DomainJobFailureCoordinator
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import Environment, WorkerSettings
from soa_worker.worker import JobFailureResult, Worker

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)
FAST = WorkerSettings(
    environment=Environment.TEST,
    poll_interval_seconds=0.01,
    heartbeat_interval_seconds=0.01,
)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/domain-failures.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _seed(
    db: DatabaseSessions,
    *,
    job_type: str,
    max_attempts: int,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with db.session_scope() as session:
        if job_type == "evaluation.run":
            record = await create_evaluation_run(
                session,
                CONTEXT,
                stream_id=uuid.uuid4(),
                candidate_fingerprint="f" * 64,
                dataset_version_id=uuid.uuid4(),
                predictions={},
                baseline_run_id=None,
                actor_id="user:test",
            )
            record.state = EvaluationRunState.RUNNING
            target_key = "evaluation_run_id"
        else:
            record = new_data_export_job(
                organization_id=ORG,
                scope="organization",
                created_by="user:test",
            )
            record.state = DataExportState.RUNNING
            session.add(record)
            await session.flush()
            target_key = "data_export_id"
        job = await enqueue_job(
            session,
            job_type=job_type,
            organization_id=ORG,
            payload={"organization_id": str(ORG), target_key: str(record.id)},
            max_attempts=max_attempts,
        )
        await session.flush()
        return record.id, job.id


async def _run_one(
    db: DatabaseSessions,
    *,
    error: Exception,
) -> None:
    registry = HandlerRegistry()

    @registry.register("evaluation.run")
    async def fail_evaluation(_job: JobEnvelope) -> None:
        raise error

    @registry.register("data_export.build")
    async def fail_data_export(_job: JobEnvelope) -> None:
        raise error

    coordinator = DomainJobFailureCoordinator(db)
    queue = DatabaseJobQueue(
        db,
        worker_id="worker:test",
        reconcile_terminal_failures=coordinator.reconcile,
        reconciliation_interval_seconds=0,
    )
    fetches = 0
    worker: Worker

    async def fetch() -> JobEnvelope | None:
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return await queue.claim()
        worker.request_stop()
        return None

    worker = Worker(
        FAST,
        registry,
        fetch_job=fetch,
        on_job_failed=queue.failed,
        on_job_terminal_failure=coordinator.handle,
        heartbeat_job=queue.heartbeat,
    )
    await worker.run()


async def test_transient_retry_keeps_domain_record_running(db: DatabaseSessions) -> None:
    record_id, job_id = await _seed(db, job_type="evaluation.run", max_attempts=2)
    await _run_one(db, error=RuntimeError("CANARY customer data"))

    async with db.session_scope() as session:
        job = await session.get(Job, job_id)
        record = await EvaluationRunRepository(session, CONTEXT).get(record_id)
        assert job is not None and job.status == JobStatus.PENDING
        assert record is not None and record.state == EvaluationRunState.RUNNING
        assert record.safe_error is None
    await db.dispose()


@pytest.mark.parametrize(
    ("job_type", "max_attempts", "error", "audit_action"),
    [
        ("evaluation.run", 5, ValueError("CANARY invalid payload"), "evaluation.failed"),
        (
            "data_export.build",
            1,
            RuntimeError("CANARY provider unavailable"),
            "data_export.failed",
        ),
    ],
)
async def test_terminal_or_exhausted_failure_marks_domain_failed_independently(
    db: DatabaseSessions,
    job_type: str,
    max_attempts: int,
    error: Exception,
    audit_action: str,
) -> None:
    record_id, job_id = await _seed(db, job_type=job_type, max_attempts=max_attempts)
    await _run_one(db, error=error)

    async with db.session_scope() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.DEAD_LETTER
        if job_type == "evaluation.run":
            record = await EvaluationRunRepository(session, CONTEXT).get(record_id)
        else:
            record = await DataExportJobRepository(session, CONTEXT).get(record_id)
        assert record is not None and record.state == "failed"
        assert record.finished_at is not None
        assert record.safe_error is not None
        assert "CANARY" not in record.safe_error
        audit = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == audit_action))
        ).scalar_one()
        assert audit.target_id == str(record_id)
        assert audit.organization_id == ORG
    await db.dispose()


async def test_queue_claim_reconciles_stale_domain_states_after_callback_crash(
    db: DatabaseSessions,
) -> None:
    evaluation_id, _ = await _seed(db, job_type="evaluation.run", max_attempts=1)
    export_id, _ = await _seed(db, job_type="data_export.build", max_attempts=1)

    # Simulate the worker dying after each queue dead-letter transaction but
    # before its domain callback. The returned terminal disposition is lost.
    queue = DatabaseJobQueue(db, worker_id="worker:crashed")
    for _ in range(2):
        envelope = await queue.claim()
        assert envelope is not None
        result = await queue.failed(envelope, RuntimeError("worker died after commit"))
        assert result is not None and result.terminal is True

    async with db.session_scope() as session:
        evaluation = await EvaluationRunRepository(session, CONTEXT).get(evaluation_id)
        export = await DataExportJobRepository(session, CONTEXT).get(export_id)
        assert evaluation is not None and evaluation.state == EvaluationRunState.RUNNING
        assert export is not None and export.state == DataExportState.RUNNING

    coordinator = DomainJobFailureCoordinator(db)

    async def reconcile_one() -> int:
        return await coordinator.reconcile(limit=1)

    repairing_queue = DatabaseJobQueue(
        db,
        worker_id="worker:replacement",
        reconcile_terminal_failures=reconcile_one,
        reconciliation_interval_seconds=0,
    )
    assert await repairing_queue.claim() is None
    assert await repairing_queue.claim() is None

    async with db.session_scope() as session:
        evaluation = await EvaluationRunRepository(session, CONTEXT).get(evaluation_id)
        export = await DataExportJobRepository(session, CONTEXT).get(export_id)
        assert evaluation is not None and evaluation.state == EvaluationRunState.FAILED
        assert export is not None and export.state == DataExportState.FAILED
    await db.dispose()


async def test_terminal_callback_cannot_cross_tenant_boundary(db: DatabaseSessions) -> None:
    evaluation_id, _ = await _seed(db, job_type="evaluation.run", max_attempts=1)
    other_organization = uuid.UUID("99999999-9999-4999-8999-999999999999")
    coordinator = DomainJobFailureCoordinator(db)
    await coordinator.handle(
        JobEnvelope(
            job_type="evaluation.run",
            organization_id=other_organization,
            payload={
                "organization_id": str(other_organization),
                "evaluation_run_id": str(evaluation_id),
            },
        ),
        JobFailureResult(terminal=True, safe_error="evaluation.run handler failed"),
    )

    async with db.session_scope() as session:
        evaluation = await EvaluationRunRepository(session, CONTEXT).get(evaluation_id)
        assert evaluation is not None and evaluation.state == EvaluationRunState.RUNNING
        assert evaluation.safe_error is None
    await db.dispose()


async def test_final_attempt_lock_expiry_is_reconciled_to_domain_failure(
    db: DatabaseSessions,
) -> None:
    evaluation_id, job_id = await _seed(db, job_type="evaluation.run", max_attempts=1)
    crashed_queue = DatabaseJobQueue(db, worker_id="worker:crashed")
    assert await crashed_queue.claim() is not None
    async with db.session_scope() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.lock_expires_at = utcnow() - timedelta(seconds=1)

    coordinator = DomainJobFailureCoordinator(db)
    replacement_queue = DatabaseJobQueue(
        db,
        worker_id="worker:replacement",
        reconcile_terminal_failures=coordinator.reconcile,
        reconciliation_interval_seconds=0,
    )
    assert await replacement_queue.claim() is None

    async with db.session_scope() as session:
        job = await session.get(Job, job_id)
        evaluation = await EvaluationRunRepository(session, CONTEXT).get(evaluation_id)
        assert job is not None and job.status == JobStatus.DEAD_LETTER
        assert evaluation is not None and evaluation.state == EvaluationRunState.FAILED
        assert "expired on final attempt" in (evaluation.safe_error or "")
    await db.dispose()


async def test_terminal_deletion_job_failure_cannot_leave_request_running(
    db: DatabaseSessions,
) -> None:
    document_id = uuid.uuid4()
    async with db.session_scope() as session:
        request = DeletionRequest(
            organization_id=ORG,
            document_id=document_id,
            state=DeletionRequestState.RUNNING.value,
            reason="verified request",
            requested_by="user:requester",
            approved_by="user:approver",
        )
        session.add(request)
        await session.flush()
        request_id = request.id

    coordinator = DomainJobFailureCoordinator(db)
    await coordinator.handle(
        JobEnvelope(
            job_type=DOCUMENT_DELETION_JOB_TYPE,
            organization_id=ORG,
            payload={
                "organization_id": str(ORG),
                "deletion_request_id": str(request_id),
            },
        ),
        JobFailureResult(
            terminal=True,
            safe_error="document.delete handler failed (RuntimeError)",
        ),
    )
    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get(request_id)
        assert request is not None
        assert request.state == DeletionRequestState.FAILED.value
        assert request.safe_error == "document.delete handler failed (RuntimeError)"
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "document.deletion_failed")
            )
        ).scalar_one()
        assert event.target_id == str(document_id)
