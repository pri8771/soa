"""Real-PostgreSQL cross-engine atomicity for global abuse controls."""

import asyncio
import os
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, text

from soa_api.services.rate_limit import DatabaseSlidingWindowRateLimiter
from soa_db import DatabaseSessions, create_database_engine
from soa_db.rate_limits import RateLimitBucket, RateLimitCounter
from soa_db.types import utcnow

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; cross-replica locks require PostgreSQL",
    ),
]


async def test_atomic_limit_is_shared_across_independent_postgres_engines() -> None:
    assert POSTGRES_URL is not None
    operation = f"atomic_{uuid.uuid4().hex}"
    identity = "raw-identity-must-never-persist"
    first_db = DatabaseSessions(create_database_engine(POSTGRES_URL))
    second_db = DatabaseSessions(create_database_engine(POSTGRES_URL))
    secret = SecretStr("postgres-rate-limit-test-key-0123456789")
    replicas = [
        DatabaseSlidingWindowRateLimiter(first_db, secret),
        DatabaseSlidingWindowRateLimiter(second_db, secret),
    ]
    try:
        current = utcnow()
        decisions = await asyncio.gather(
            *(replicas[index % 2].check(operation, identity, 7, now=current) for index in range(30))
        )
        assert sum(decision.allowed for decision in decisions) == 7
        assert (await replicas[0].snapshot())[operation] == {"allowed": 7, "denied": 23}

        async with first_db.session_scope() as session:
            persisted = await session.scalar(
                text("SELECT identity_hash FROM rate_limit_buckets WHERE operation = :operation"),
                {"operation": operation},
            )
            assert isinstance(persisted, str) and len(persisted) == 64
            assert identity not in persisted
            rls = dict(
                (
                    await session.execute(
                        text(
                            "SELECT relname, relrowsecurity FROM pg_class "
                            "WHERE relname IN ('rate_limit_buckets', 'rate_limit_counters')"
                        )
                    )
                ).all()
            )
            assert rls == {"rate_limit_buckets": False, "rate_limit_counters": False}
    finally:
        async with first_db.session_scope() as session:
            await session.execute(
                delete(RateLimitBucket).where(RateLimitBucket.operation == operation)
            )
            await session.execute(
                delete(RateLimitCounter).where(RateLimitCounter.operation == operation)
            )
        await first_db.dispose()
        await second_db.dispose()
