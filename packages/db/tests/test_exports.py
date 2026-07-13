"""Export-job and delivery-attempt tests (EXP-005): business-key
idempotency, the fixed payload reference across retries, the state
machine, and append-only attempts."""

import uuid
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.exports import (
    DeliveryAttemptImmutableError,
    DeliveryAttemptRepository,
    ExportJobRepository,
    ExportJobState,
    InvalidExportTransitionError,
    create_export_job,
    export_business_key,
    record_delivery_attempt,
    transition_export_job,
)
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN = uuid.UUID("55555555-5555-4555-8555-555555555555")
PAYLOAD = uuid.UUID("66666666-6666-4666-8666-666666666666")
INTEGRATION = uuid.UUID("77777777-7777-4777-8777-777777777777")
MAPPING = uuid.UUID("88888888-8888-4888-8888-888888888888")
CONTEXT = OrganizationContext(organization_id=ORG_A)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/exports.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_job(db: DatabaseSessions) -> uuid.UUID:
    async with db.session_scope() as session:
        job = await create_export_job(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            canonical_payload_id=PAYLOAD,
            integration_id=INTEGRATION,
            mapping_version_id=MAPPING,
            actor_id="system:export",
        )
        return job.id


async def test_business_key_makes_creation_idempotent(db: DatabaseSessions) -> None:
    job_id = await make_job(db)
    async with db.session_scope() as session:
        again = await create_export_job(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            canonical_payload_id=PAYLOAD,
            integration_id=INTEGRATION,
            mapping_version_id=MAPPING,
            actor_id="system:export",
        )
        assert again.id == job_id  # the same intent, not a duplicate
        assert again.business_key == export_business_key(INTEGRATION, DOC, RUN)
        assert await ExportJobRepository(session, CONTEXT).count() == 1


async def test_payload_and_mapping_stay_fixed_across_retries(db: DatabaseSessions) -> None:
    job_id = await make_job(db)
    async with db.session_scope() as session:
        repo = ExportJobRepository(session, CONTEXT)
        job = await repo.get(job_id)
        assert job is not None
        await transition_export_job(
            session, CONTEXT, job=job, to_state=ExportJobState.IN_PROGRESS, actor_id="system:x"
        )
        await record_delivery_attempt(
            session, CONTEXT, job=job, outcome="retryable_error", safe_error="receiver timeout"
        )
        await transition_export_job(
            session,
            CONTEXT,
            job=job,
            to_state=ExportJobState.FAILED_RETRYABLE,
            actor_id="system:x",
        )
        # Retry: back to pending — SAME payload, SAME mapping, SAME key.
        await transition_export_job(
            session, CONTEXT, job=job, to_state=ExportJobState.PENDING, actor_id="system:x"
        )
        assert job.canonical_payload_id == PAYLOAD
        assert job.mapping_version_id == MAPPING
        assert job.business_key == export_business_key(INTEGRATION, DOC, RUN)
        assert job.attempt_count == 1


async def test_state_machine_refuses_illegal_transitions(db: DatabaseSessions) -> None:
    job_id = await make_job(db)
    async with db.session_scope() as session:
        job = await ExportJobRepository(session, CONTEXT).get(job_id)
        assert job is not None
        with pytest.raises(InvalidExportTransitionError):
            await transition_export_job(
                session, CONTEXT, job=job, to_state=ExportJobState.SUCCEEDED, actor_id="system:x"
            )
        await transition_export_job(
            session, CONTEXT, job=job, to_state=ExportJobState.IN_PROGRESS, actor_id="system:x"
        )
        await transition_export_job(
            session, CONTEXT, job=job, to_state=ExportJobState.SUCCEEDED, actor_id="system:x"
        )
        # Succeeded is terminal.
        with pytest.raises(InvalidExportTransitionError):
            await transition_export_job(
                session, CONTEXT, job=job, to_state=ExportJobState.PENDING, actor_id="system:x"
            )


async def test_attempts_are_append_only_and_numbered(db: DatabaseSessions) -> None:
    job_id = await make_job(db)
    async with db.session_scope() as session:
        job = await ExportJobRepository(session, CONTEXT).get(job_id)
        assert job is not None
        first = await record_delivery_attempt(
            session,
            CONTEXT,
            job=job,
            outcome="retryable_error",
            response_status=503,
            safe_error="receiver returned 503",
            request_sha256="a" * 64,
        )
        second = await record_delivery_attempt(
            session, CONTEXT, job=job, outcome="delivered", response_status=200
        )
        assert (first.attempt_number, second.attempt_number) == (1, 2)
        assert job.attempt_count == 2
        with pytest.raises(ValueError, match="attempt outcomes"):
            await record_delivery_attempt(session, CONTEXT, job=job, outcome="shrug")
        attempt_id = first.id

    # Editing or deleting an attempt is refused; history survives.
    with pytest.raises(DeliveryAttemptImmutableError):
        async with db.session_scope() as session:
            row = await DeliveryAttemptRepository(session, CONTEXT).get(attempt_id)
            assert row is not None
            row.outcome = "delivered"
            await session.flush()
    with pytest.raises(DeliveryAttemptImmutableError):
        async with db.session_scope() as session:
            row = await DeliveryAttemptRepository(session, CONTEXT).get(attempt_id)
            assert row is not None
            await session.delete(row)
            await session.flush()
    async with db.session_scope() as session:
        job = await ExportJobRepository(session, CONTEXT).get(job_id)
        assert job is not None
        attempts = await DeliveryAttemptRepository(session, CONTEXT).list_for_job(job.id)
        assert [(a.attempt_number, a.outcome) for a in attempts] == [
            (1, "retryable_error"),
            (2, "delivered"),
        ]


async def test_exports_are_tenant_scoped(db: DatabaseSessions) -> None:
    job_id = await make_job(db)
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await ExportJobRepository(session, other).get(job_id) is None

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    for table in ("export_jobs", "delivery_attempts"):
        assert table in RLS_PROTECTED_TABLES
