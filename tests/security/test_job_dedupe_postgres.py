"""Real-PostgreSQL proof that queue dedupe is atomic across replicas."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select

from soa_db import DatabaseSessions, create_database_engine
from soa_db.jobs import Job, enqueue_job

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; queue dedupe requires PostgreSQL",
    ),
]


async def test_concurrent_replicas_create_one_deduplicated_job() -> None:
    assert POSTGRES_URL is not None
    key = f"postgres-dedupe:{uuid.uuid4()}"
    first = DatabaseSessions(create_database_engine(POSTGRES_URL))
    second = DatabaseSessions(create_database_engine(POSTGRES_URL))

    async def enqueue(index: int) -> uuid.UUID:
        sessions = first if index % 2 == 0 else second
        async with sessions.session_scope() as session:
            job = await enqueue_job(
                session,
                job_type="test.atomic-dedupe",
                payload={"same": True},
                dedupe_key=key,
            )
            return job.id

    try:
        ids = await asyncio.gather(*(enqueue(index) for index in range(20)))
        assert len(set(ids)) == 1
        async with first.session_scope() as session:
            rows = list((await session.execute(select(Job).where(Job.dedupe_key == key))).scalars())
            assert len(rows) == 1
    finally:
        async with first.session_scope() as session:
            await session.execute(delete(Job).where(Job.dedupe_key == key))
        await first.dispose()
        await second.dispose()
