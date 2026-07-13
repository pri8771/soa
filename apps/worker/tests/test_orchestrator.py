"""Orchestrator tests (PRC-003): full mock workflow, cancellation, retry
classification, duplicate messages, restart resume, fixed config."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import (
    Document,
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import Job, JobStatus
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun, ProcessingRunRepository, StageRun, StageRunRepository
from soa_worker.orchestrator import (
    STAGE_SEQUENCE,
    Orchestrator,
    StageExecutionError,
    StageOutcome,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
STREAM_VERSION = uuid.UUID("44444444-4444-4444-8444-444444444444")
CONTEXT = OrganizationContext(organization_id=ORG)
SHA = "b" * 64


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/orchestrator.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_queued_document(db: DatabaseSessions) -> uuid.UUID:
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=SHA,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker",
        )
        await transition_document(
            session, CONTEXT, document=document, to_state=DocumentState.QUEUED, actor_id="worker"
        )
        return document.id


def make_executors(
    *,
    route: str = "approved",
    fail_stage_name: str | None = None,
    fail_retryable: bool = True,
    fail_times: int = 10_000,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Deterministic mock executors; counts record executions per stage."""
    counts: dict[str, int] = {}
    failures_left = {"n": fail_times}

    def executor_for(stage: str):
        async def run(
            session: AsyncSession,
            context: OrganizationContext,
            run_row: ProcessingRun,
            document: Document,
            stage_run: StageRun,
        ) -> StageOutcome:
            counts[stage] = counts.get(stage, 0) + 1
            if stage == fail_stage_name and failures_left["n"] > 0:
                failures_left["n"] -= 1
                raise StageExecutionError("mock failure (safe message)", retryable=fail_retryable)
            return StageOutcome(
                output_summary={"stage": stage, "ok": True},
                cost_cents=2,
                route=route if stage == STAGE_SEQUENCE[-1] else None,
            )

        return run

    return {stage: executor_for(stage) for stage in STAGE_SEQUENCE}, counts


async def pump(db: DatabaseSessions, orchestrator: Orchestrator, *, deliveries: int = 100) -> int:
    """Deliver pending stage jobs like the worker would, one at a time.
    Returns how many deliveries happened."""
    delivered = 0
    processed: set[uuid.UUID] = set()
    for _ in range(deliveries):
        async with db.session_scope() as session:
            jobs = (
                (
                    await session.execute(
                        select(Job).where(
                            Job.job_type == "document.stage",
                            Job.status == JobStatus.PENDING.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            pending = [j for j in jobs if j.id not in processed]
            if not pending:
                return delivered
            job = pending[0]
            processed.add(job.id)
            payload = dict(job.payload)
        await orchestrator.handle_stage(payload)
        delivered += 1
    return delivered


def preprocess_payload(document_id: uuid.UUID) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "organization_id": str(ORG),
        "stream_id": str(STREAM),
        "stream_version_id": str(STREAM_VERSION),
        "config_fingerprint": "f" * 64,
    }


async def test_full_mock_workflow_reaches_approved(db: DatabaseSessions) -> None:
    document_id = await seed_queued_document(db)
    executors, counts = make_executors(route="approved")
    orchestrator = Orchestrator(db, executors)

    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert run.state == "succeeded"
        # Configuration snapshot fixed per run, from the enqueue-time pin.
        assert run.stream_version_id == STREAM_VERSION
        assert run.config_fingerprint == "f" * 64
        assert run.total_cost_cents == 2 * len(STAGE_SEQUENCE)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        assert sorted({s.stage for s in stages}) == sorted(STAGE_SEQUENCE)
        assert all(s.state == "succeeded" for s in stages)
    assert all(counts[stage] == 1 for stage in STAGE_SEQUENCE), "each stage ran exactly once"

    # A duplicate preprocess delivery is a no-op (document moved on).
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    async with db.session_scope() as session:
        runs = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert len(runs) == 1


async def test_review_route_parks_in_review_required(db: DatabaseSessions) -> None:
    document_id = await seed_queued_document(db)
    executors, _counts = make_executors(route="review_required")
    orchestrator = Orchestrator(db, executors)
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "review_required"


async def test_retryable_failure_retries_with_new_attempt_then_succeeds(
    db: DatabaseSessions,
) -> None:
    document_id = await seed_queued_document(db)
    executors, counts = make_executors(fail_stage_name="extracting", fail_times=1)
    orchestrator = Orchestrator(db, executors)
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        extracting = [s for s in stages if s.stage == "extracting"]
        assert [(s.attempt, s.state) for s in extracting] == [(1, "failed"), (2, "succeeded")]
        assert extracting[0].safe_error == "mock failure (safe message)"
        assert extracting[0].failure_class == "retryable"
    assert counts["extracting"] == 2


async def test_exhausted_retries_fail_the_run_and_park_the_document(
    db: DatabaseSessions,
) -> None:
    document_id = await seed_queued_document(db)
    executors, counts = make_executors(fail_stage_name="classifying", fail_retryable=True)
    orchestrator = Orchestrator(db, executors, max_stage_attempts=2)
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "failed_retryable"
        assert "classifying failed" in (document.state_reason or "")
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert run.state == "failed"
    assert counts["classifying"] == 2


async def test_terminal_failure_stops_without_retry(db: DatabaseSessions) -> None:
    document_id = await seed_queued_document(db)
    executors, counts = make_executors(fail_stage_name="preprocessing", fail_retryable=False)
    orchestrator = Orchestrator(db, executors)
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "failed_terminal"
    assert counts["preprocessing"] == 1


async def test_cancellation_finishes_the_run_without_more_work(db: DatabaseSessions) -> None:
    document_id = await seed_queued_document(db)
    executors, counts = make_executors()
    orchestrator = Orchestrator(db, executors)
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    # Run exactly one stage, then cancel the document.
    await pump(db, orchestrator, deliveries=1)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.CANCELLED,
            reason="user cancelled",
            actor_id="user:reviewer",
        )
    await pump(db, orchestrator)
    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert run.state == "cancelled"
    assert counts.get("classifying") is None, "no stage ran after cancellation"


async def test_duplicate_stage_messages_are_noops_that_still_advance(
    db: DatabaseSessions,
) -> None:
    document_id = await seed_queued_document(db)
    executors, counts = make_executors()
    orchestrator = Orchestrator(db, executors)
    await orchestrator.handle_preprocess(preprocess_payload(document_id))

    # Deliver the FIRST stage job twice (redelivery after a crash that
    # committed): the stage executes once, and progress still happens.
    async with db.session_scope() as session:
        job = (
            (await session.execute(select(Job).where(Job.job_type == "document.stage")))
            .scalars()
            .one()
        )
        payload = dict(job.payload)
    await orchestrator.handle_stage(payload)
    await orchestrator.handle_stage(payload)  # duplicate
    assert counts["preprocessing"] == 1

    await pump(db, orchestrator)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        assert len([s for s in stages if s.stage == "preprocessing"]) == 1
