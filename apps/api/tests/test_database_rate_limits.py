import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select

from soa_api.app import create_app
from soa_api.services.rate_limit import (
    DatabaseSlidingWindowRateLimiter,
    SlidingWindowRateLimiter,
)
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.rate_limits import RateLimitBucket, RateLimitCounter
from soa_db.types import utcnow
from soa_storage import MemoryObjectStore

HASH_SECRET = SecretStr("rate-limit-test-key-0123456789abcdef")


@pytest.fixture
async def database(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/rate-limits.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = DatabaseSessions(engine)
    yield sessions
    await sessions.dispose()


async def test_atomic_budget_is_shared_across_limiter_instances(
    database: DatabaseSessions,
) -> None:
    replicas = [
        DatabaseSlidingWindowRateLimiter(database, HASH_SECRET),
        DatabaseSlidingWindowRateLimiter(database, HASH_SECRET),
    ]
    current = utcnow()
    decisions = await asyncio.gather(
        *(
            replicas[index % len(replicas)].check(
                "uploads", "user:private-customer-identity", 5, now=current
            )
            for index in range(20)
        )
    )

    assert sum(decision.allowed for decision in decisions) == 5
    assert all(decision.retry_after_seconds == 60 for decision in decisions if not decision.allowed)
    async with database.session_scope() as session:
        bucket = (await session.execute(select(RateLimitBucket))).scalar_one()
        counter = (await session.execute(select(RateLimitCounter))).scalar_one()
        assert len(bucket.events) == 5
        assert len(bucket.identity_hash) == 64
        assert bucket.identity_hash != "user:private-customer-identity"
        assert counter.allowed == 5
        assert counter.denied == 15
        persisted = str(bucket.__dict__)
        assert "private-customer-identity" not in persisted

    assert await replicas[1].snapshot() == {"uploads": {"allowed": 5, "denied": 15}}


async def test_denial_does_not_extend_retention_and_cleanup_is_bounded(
    database: DatabaseSessions,
) -> None:
    limiter = DatabaseSlidingWindowRateLimiter(
        database,
        HASH_SECRET,
        cleanup_batch_size=1,
    )
    started = utcnow()
    assert (await limiter.check("uploads", "first", 1, now=started)).allowed
    denied = await limiter.check("uploads", "first", 1, now=started + timedelta(seconds=1))
    assert not denied.allowed
    async with database.session_scope() as session:
        first_expiry = await session.scalar(select(RateLimitBucket.expires_at))
    assert first_expiry == started + timedelta(seconds=60)

    # Two expired identities are removed one per subsequent decision, proving
    # cleanup work is capped rather than an unbounded request-path sweep.
    assert (await limiter.check("uploads", "second", 1, now=started)).allowed
    later = started + timedelta(seconds=61)
    assert (await limiter.check("uploads", "third", 1, now=later)).allowed
    async with database.session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(RateLimitBucket)) == 2
    assert (await limiter.check("uploads", "fourth", 1, now=later)).allowed
    async with database.session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(RateLimitBucket)) == 2


async def test_durable_logs_and_operation_scoped_hashes_never_contain_raw_identity(
    database: DatabaseSessions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = DatabaseSlidingWindowRateLimiter(database, HASH_SECRET)
    identity = "tenant-and-user-private-value"
    current = utcnow()
    await limiter.check("uploads", identity, 1, now=current)
    await limiter.check("uploads", identity, 1, now=current)
    await limiter.check("replays", identity, 1, now=current)

    async with database.session_scope() as session:
        hashes = list((await session.execute(select(RateLimitBucket.identity_hash))).scalars())
    assert len(hashes) == 2
    assert len(set(hashes)) == 2
    assert all(identity not in digest for digest in hashes)
    assert all(identity not in repr(record.__dict__) for record in caplog.records)


async def test_backend_failure_fails_closed_with_retry_metadata(tmp_path: Path) -> None:
    # Deliberately do not create the limiter tables.
    database = DatabaseSessions(
        create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/missing-rate-limit-schema.db")
    )
    limiter = DatabaseSlidingWindowRateLimiter(database, HASH_SECRET)
    assert await limiter.ready() is False
    with pytest.raises(HTTPException) as exc_info:
        await limiter.enforce("uploads", "user:test", 1)
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "1"}
    await database.dispose()


async def test_app_selects_database_backend_outside_dev_and_test(
    database: DatabaseSessions,
) -> None:
    settings = ApiSettings(
        environment=Environment.STAGING,
        auth_dev_mode=False,
        secret_key=SecretStr("staging-rate-limit-key-0123456789abcdef"),
        database_url=SecretStr("postgresql+asyncpg://service:password@db/soa"),
    )
    app = create_app(settings, db=database, object_store=MemoryObjectStore())
    assert isinstance(app.state.dependencies.rate_limiter, DatabaseSlidingWindowRateLimiter)
    assert await app.state.dependencies.rate_limiter.ready() is True
    readiness_names = {
        result.name for result in await app.state.dependencies.run_readiness_checks()
    }
    assert "rate-limiter" in readiness_names

    test_app = create_app(
        ApiSettings(environment=Environment.TEST),
        db=database,
        object_store=MemoryObjectStore(),
    )
    assert isinstance(test_app.state.dependencies.rate_limiter, SlidingWindowRateLimiter)
