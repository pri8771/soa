"""Failed external compensations become durable without retaining values."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.external_cleanup import (
    EXTERNAL_CLEANUP_JOB_TYPE,
    ExternalCleanupIntent,
    ExternalCleanupState,
    ExternalResourceType,
    complete_external_cleanup_intent_for_locator,
    register_external_resource_rollback,
    stage_external_cleanup_intent,
)
from soa_db.jobs import Job, JobStatus

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
OBJECT_KEY = f"orgs/{ORG}/documents/{uuid.UUID(int=3)}/original/token-order.pdf"


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/external-cleanup.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_failed_rollback_compensation_persists_intent_and_queue_job(
    db: DatabaseSessions,
) -> None:
    async def cleanup() -> None:
        raise RuntimeError("CANARY-super-secret-provider-message")

    with pytest.raises(ValueError, match="owning transaction failed"):
        async with db.session_scope() as session:
            register_external_resource_rollback(
                session,
                organization_id=ORG,
                resource_type=ExternalResourceType.OBJECT,
                resource_locator=OBJECT_KEY,
                cleanup=cleanup,
            )
            raise ValueError("owning transaction failed")

    async with db.session_scope() as session:
        intent = (await session.execute(select(ExternalCleanupIntent))).scalar_one()
        job = (await session.execute(select(Job))).scalar_one()
        assert intent.organization_id == ORG
        assert intent.resource_locator == OBJECT_KEY
        assert intent.state == ExternalCleanupState.PENDING.value
        assert intent.attempts == 0
        assert intent.dispatch_count == 1
        assert intent.last_error == "object cleanup failed (RuntimeError)"
        assert "CANARY" not in intent.last_error
        assert job.job_type == EXTERNAL_CLEANUP_JOB_TYPE
        assert job.status == JobStatus.PENDING
        assert job.organization_id == ORG
        assert job.payload == {
            "organization_id": str(ORG),
            "cleanup_intent_id": str(intent.id),
            "dispatch": 1,
        }
        assert OBJECT_KEY not in str(job.payload)
    await db.dispose()


async def test_successful_rollback_compensation_needs_no_durable_intent(
    db: DatabaseSessions,
) -> None:
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(RuntimeError, match="rollback"):
        async with db.session_scope() as session:
            register_external_resource_rollback(
                session,
                organization_id=ORG,
                resource_type=ExternalResourceType.OBJECT,
                resource_locator=OBJECT_KEY,
                cleanup=cleanup,
            )
            raise RuntimeError("rollback")

    assert calls == 1
    async with db.session_scope() as session:
        assert (await session.execute(select(ExternalCleanupIntent))).scalars().all() == []
        assert (await session.execute(select(Job))).scalars().all() == []
    await db.dispose()


async def test_registration_rejects_cross_tenant_resource_locator(
    db: DatabaseSessions,
) -> None:
    async def cleanup() -> None:
        return None

    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="outside the job tenant"):
            register_external_resource_rollback(
                session,
                organization_id=OTHER_ORG,
                resource_type=ExternalResourceType.OBJECT,
                resource_locator=OBJECT_KEY,
                cleanup=cleanup,
            )
    await db.dispose()


async def test_independent_deletion_completes_intent_and_erases_locator(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        intent = await stage_external_cleanup_intent(
            session,
            organization_id=ORG,
            resource_type=ExternalResourceType.OBJECT,
            resource_locator=OBJECT_KEY,
            safe_error="object cleanup failed (RuntimeError)",
        )
        intent_id = intent.id

    async with db.session_scope() as session:
        assert await complete_external_cleanup_intent_for_locator(
            session,
            organization_id=ORG,
            resource_type=ExternalResourceType.OBJECT,
            resource_locator=OBJECT_KEY,
        )

    async with db.session_scope() as session:
        intent = await session.get(ExternalCleanupIntent, intent_id)
        assert intent is not None
        assert intent.state == ExternalCleanupState.COMPLETED.value
        assert intent.resource_locator is None
        assert intent.last_error is None
        assert intent.completed_at is not None
    await db.dispose()
