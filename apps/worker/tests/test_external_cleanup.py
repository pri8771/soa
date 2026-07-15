"""External cleanup retries are tenant-fenced, idempotent, and bounded."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_config import MemorySecretStore, SecretNotFoundError
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.external_cleanup import (
    EXTERNAL_CLEANUP_JOB_TYPE,
    ExternalCleanupIntent,
    ExternalCleanupState,
    ExternalResourceType,
    stage_external_cleanup_intent,
)
from soa_db.jobs import FailureClass, Job, JobStatus, claim_next_jobs, mark_failed
from soa_storage import MemoryObjectStore
from soa_worker.external_cleanup import (
    ExternalCleanupCoordinator,
    ExternalCleanupRetryError,
    register_external_cleanup_handler,
)
from soa_worker.registry import HandlerRegistry, JobEnvelope

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
OBJECT_KEY = f"orgs/{ORG}/documents/{uuid.UUID(int=3)}/original/token-order.pdf"


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/worker-cleanup.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _seed_intent(
    db: DatabaseSessions,
    *,
    resource_type: ExternalResourceType,
    locator: str,
) -> uuid.UUID:
    async with db.session_scope() as session:
        intent = await stage_external_cleanup_intent(
            session,
            organization_id=ORG,
            resource_type=resource_type,
            resource_locator=locator,
            safe_error="initial compensation failed (RuntimeError)",
        )
        return intent.id


def _envelope(intent_id: uuid.UUID, *, organization_id: uuid.UUID = ORG) -> JobEnvelope:
    return JobEnvelope(
        job_type=EXTERNAL_CLEANUP_JOB_TYPE,
        organization_id=organization_id,
        payload={
            "organization_id": str(organization_id),
            "cleanup_intent_id": str(intent_id),
            "dispatch": 1,
        },
    )


class FailingOnceObjectStore(MemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def delete(self, key: str) -> None:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("CANARY object provider diagnostic")
        await super().delete(key)


async def test_handler_records_safe_failure_then_completes_idempotently(
    db: DatabaseSessions,
) -> None:
    store = FailingOnceObjectStore()
    await store.put(OBJECT_KEY, b"payload")
    intent_id = await _seed_intent(
        db,
        resource_type=ExternalResourceType.OBJECT,
        locator=OBJECT_KEY,
    )
    registry = HandlerRegistry()
    register_external_cleanup_handler(registry, db, store, MemorySecretStore())
    handler = registry.resolve(EXTERNAL_CLEANUP_JOB_TYPE)

    with pytest.raises(ExternalCleanupRetryError, match="object cleanup failed") as captured:
        await handler(_envelope(intent_id))
    assert "CANARY" not in str(captured.value)
    async with db.session_scope() as session:
        intent = await session.get(ExternalCleanupIntent, intent_id)
        assert intent is not None
        assert intent.state == ExternalCleanupState.PENDING.value
        assert intent.attempts == 1
        assert intent.last_error == "object cleanup failed (RuntimeError)"
        assert "CANARY" not in intent.last_error

    await handler(_envelope(intent_id))
    async with db.session_scope() as session:
        intent = await session.get(ExternalCleanupIntent, intent_id)
        assert intent is not None
        assert intent.state == ExternalCleanupState.COMPLETED.value
        assert intent.attempts == 2
        assert intent.completed_at is not None
        assert intent.last_error is None
        assert intent.resource_locator is None
    assert await store.list_keys() == []

    # Queue redelivery after completion performs no extra provider operation.
    await handler(_envelope(intent_id))
    await db.dispose()


async def test_secret_handler_stores_reference_only_and_revokes_value(
    db: DatabaseSessions,
) -> None:
    secrets = MemorySecretStore()
    reference = await secrets.put(
        f"orgs/{ORG}/providers/anthropic/credentials/{uuid.uuid4()}",
        "CANARY actual secret value",
    )
    intent_id = await _seed_intent(
        db,
        resource_type=ExternalResourceType.SECRET,
        locator=str(reference),
    )
    registry = HandlerRegistry()
    register_external_cleanup_handler(registry, db, MemoryObjectStore(), secrets)
    await registry.resolve(EXTERNAL_CLEANUP_JOB_TYPE)(_envelope(intent_id))

    with pytest.raises(SecretNotFoundError):
        await secrets.resolve(reference)
    async with db.session_scope() as session:
        intent = await session.get(ExternalCleanupIntent, intent_id)
        job = (await session.execute(select(Job))).scalars().first()
        assert intent is not None and intent.resource_locator is None
        assert job is not None and str(reference) not in str(job.payload)
    await db.dispose()


async def test_handler_cannot_cross_tenant_boundary(db: DatabaseSessions) -> None:
    intent_id = await _seed_intent(
        db,
        resource_type=ExternalResourceType.OBJECT,
        locator=OBJECT_KEY,
    )
    registry = HandlerRegistry()
    register_external_cleanup_handler(
        registry,
        db,
        MemoryObjectStore(),
        MemorySecretStore(),
    )
    with pytest.raises(ValueError, match="does not belong"):
        await registry.resolve(EXTERNAL_CLEANUP_JOB_TYPE)(
            _envelope(intent_id, organization_id=OTHER_ORG)
        )
    await db.dispose()


async def _dead_letter_next_cleanup_job(db: DatabaseSessions, number: int) -> uuid.UUID:
    worker_id = f"worker:{number}"
    async with db.session_scope() as session:
        claimed = await claim_next_jobs(
            session,
            worker_id=worker_id,
            job_types=[EXTERNAL_CLEANUP_JOB_TYPE],
        )
        assert len(claimed) == 1
        job = claimed[0]
        mark_failed(
            job,
            worker_id=worker_id,
            error="external cleanup handler failed",
            failure_class=FailureClass.PERMANENT,
        )
        return job.id


async def test_dead_letter_sweep_redrives_twice_then_requires_manual_action(
    db: DatabaseSessions,
) -> None:
    intent_id = await _seed_intent(
        db,
        resource_type=ExternalResourceType.OBJECT,
        locator=OBJECT_KEY,
    )
    coordinator = ExternalCleanupCoordinator(db)

    for dispatch in (1, 2, 3):
        await _dead_letter_next_cleanup_job(db, dispatch)
        assert await coordinator.reconcile() == 1
        async with db.session_scope() as session:
            intent = await session.get(ExternalCleanupIntent, intent_id)
            assert intent is not None
            if dispatch < 3:
                assert intent.state == ExternalCleanupState.PENDING.value
                assert intent.dispatch_count == dispatch + 1
            else:
                assert intent.state == ExternalCleanupState.MANUAL_INTERVENTION.value
                assert intent.dispatch_count == 3

    async with db.session_scope() as session:
        jobs = list((await session.execute(select(Job).order_by(Job.created_at))).scalars())
        assert len(jobs) == 3
        assert all(job.status == JobStatus.DEAD_LETTER for job in jobs)
    await db.dispose()
