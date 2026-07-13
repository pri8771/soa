"""Processing-run tests (PRC-001): lifecycle, reprocess semantics,
succeeded-stage immutability, duplicate-execution refusal, tenant scope."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext
from soa_db.runs import (
    ProcessingRunRepository,
    RunState,
    RunStateError,
    StageFailureClass,
    StageImmutableError,
    StageRun,
    StageRunRepository,
    complete_stage,
    fail_stage,
    finish_run,
    start_run,
    start_stage,
)

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONTEXT = OrganizationContext(organization_id=ORG_A)
SHA = "e" * 64


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/runs.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_run_and_stage_lifecycle_rolls_up_metrics(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        run = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=uuid.uuid4(),
            config_fingerprint="f" * 64,
            triggered_by="system:orchestrator",
        )
        render = await start_stage(session, CONTEXT, run=run, stage="render")
        await complete_stage(
            session,
            run=run,
            stage_run=render,
            latency_ms=120,
            cost_cents=3,
            output_summary={"pages": 2},
        )
        extract = await start_stage(session, CONTEXT, run=run, stage="extract", provider="mock")
        await complete_stage(session, run=run, stage_run=extract, latency_ms=480, cost_cents=12)
        await finish_run(
            session, CONTEXT, run=run, state=RunState.SUCCEEDED, actor_id="system:orchestrator"
        )
        run_id = run.id

    async with db.session_scope() as session:
        stored = await ProcessingRunRepository(session, CONTEXT).get(run_id)
        assert stored is not None
        assert stored.state == "succeeded"
        assert stored.total_latency_ms == 600
        assert stored.total_cost_cents == 15
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run_id)
        assert [(s.stage, s.attempt, s.state) for s in stages] == [
            ("extract", 1, "succeeded"),
            ("render", 1, "succeeded"),
        ]
        assert stages[1].output_summary == {"pages": 2}


async def test_reprocess_creates_a_new_run(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        first = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="system:orchestrator",
        )
        await finish_run(
            session, CONTEXT, run=first, state=RunState.FAILED, actor_id="system:orchestrator"
        )
        second = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="user:reviewer",
            reason="reprocess after config fix",
        )
        assert (first.run_number, second.run_number) == (1, 2)
        assert first.state == "failed", "the old run is history, not mutated"


async def test_succeeded_stage_results_are_immutable(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        run = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="system:orchestrator",
        )
        stage = await start_stage(session, CONTEXT, run=run, stage="render")
        await complete_stage(session, run=run, stage_run=stage, latency_ms=10)
        stage_id = stage.id
        run_id = run.id

    with pytest.raises(StageImmutableError):
        async with db.session_scope() as session:
            stored = await StageRunRepository(session, CONTEXT).get(stage_id)
            assert stored is not None
            stored.output_summary = {"pages": 999}
            await session.flush()

    # Completing or failing it again is refused too.
    async with db.session_scope() as session:
        run = await ProcessingRunRepository(session, CONTEXT).get(run_id)
        stored = await StageRunRepository(session, CONTEXT).get(stage_id)
        assert run is not None and stored is not None
        with pytest.raises(RunStateError):
            await complete_stage(session, run=run, stage_run=stored, latency_ms=1)
        with pytest.raises(RunStateError):
            await fail_stage(
                session,
                stage_run=stored,
                safe_error="late failure",
                failure_class=StageFailureClass.TERMINAL,
            )


async def test_duplicate_stage_execution_is_refused_by_the_database(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        run = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="system:orchestrator",
        )
        await start_stage(session, CONTEXT, run=run, stage="extract")
        run_id = run.id

    # A concurrent duplicate that computed the same attempt number cannot
    # record: (run, stage, attempt) is unique.
    with pytest.raises(IntegrityError):
        async with db.session_scope() as session:
            session.add(StageRun(organization_id=ORG_A, run_id=run_id, stage="extract", attempt=1))
            await session.flush()

    # Retries are NEW attempts.
    async with db.session_scope() as session:
        run = await ProcessingRunRepository(session, CONTEXT).get(run_id)
        assert run is not None
        first = (await StageRunRepository(session, CONTEXT).list_for_run(run_id))[0]
        await fail_stage(
            session,
            stage_run=first,
            safe_error="provider timeout",
            failure_class=StageFailureClass.RETRYABLE,
        )
        retry = await start_stage(session, CONTEXT, run=run, stage="extract")
        assert retry.attempt == 2


async def test_terminal_runs_are_frozen_and_stages_refuse_to_start(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        run = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="system:orchestrator",
        )
        await finish_run(
            session, CONTEXT, run=run, state=RunState.CANCELLED, actor_id="user:reviewer"
        )
        with pytest.raises(RunStateError):
            await start_stage(session, CONTEXT, run=run, stage="render")
        run_id = run.id

    with pytest.raises(RunStateError):
        async with db.session_scope() as session:
            stored = await ProcessingRunRepository(session, CONTEXT).get(run_id)
            assert stored is not None
            stored.state = RunState.RUNNING.value
            await session.flush()


async def test_runs_are_tenant_scoped(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        run = await start_run(
            session,
            CONTEXT,
            document_id=DOC,
            input_sha256=SHA,
            stream_version_id=None,
            config_fingerprint=None,
            triggered_by="system:orchestrator",
        )
        run_id = run.id
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await ProcessingRunRepository(session, other).get(run_id) is None
        assert await ProcessingRunRepository(session, other).list_for_document(DOC) == []

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert "processing_runs" in RLS_PROTECTED_TABLES
    assert "stage_runs" in RLS_PROTECTED_TABLES
