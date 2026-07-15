"""Real-PostgreSQL atomicity and RLS for provider routing metrics."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select

from soa_db import DatabaseSessions, create_database_engine
from soa_db.provider_metrics import ProviderRuntimeMetric, record_provider_attempt
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; provider metric test requires PostgreSQL",
    ),
]


async def test_concurrent_replicas_keep_every_attempt_and_rls_hides_other_tenants() -> None:
    assert POSTGRES_URL is not None
    organization_id = uuid.uuid4()
    other_id = uuid.uuid4()
    context = OrganizationContext(organization_id)
    # Independent engines model separate API/worker replicas rather than
    # concurrent coroutines sharing one SQLAlchemy session.
    first = DatabaseSessions(create_database_engine(POSTGRES_URL))
    second = DatabaseSessions(create_database_engine(POSTGRES_URL))

    async def record(index: int) -> None:
        sessions = first if index % 2 == 0 else second
        async with sessions.session_scope() as session:
            await bind_tenant(session, organization_id)
            await record_provider_attempt(
                session,
                context,
                capability="field_extraction",
                provider="postgres-concurrency-test",
                succeeded=index % 3 != 0,
                fallback=index % 2 == 1,
                latency_ms=index + 1,
                failure_class="retryable" if index % 3 == 0 else None,
            )

    try:
        await asyncio.gather(*(record(index) for index in range(30)))
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            metric = (
                await session.execute(
                    select(ProviderRuntimeMetric).where(
                        ProviderRuntimeMetric.provider == "postgres-concurrency-test"
                    )
                )
            ).scalar_one()
            assert metric.attempt_count == 30
            assert metric.success_count == 20
            assert metric.failure_count == 10
            assert metric.fallback_count == 15

        async with first.session_scope() as session:
            await bind_tenant(session, other_id)
            assert (
                await session.execute(
                    select(ProviderRuntimeMetric).where(
                        ProviderRuntimeMetric.provider == "postgres-concurrency-test"
                    )
                )
            ).scalars().all() == []

        async with first.session_scope() as session:
            # No tenant GUC: FORCE RLS must fail closed even for a raw query.
            assert (
                await session.execute(
                    select(ProviderRuntimeMetric).where(
                        ProviderRuntimeMetric.provider == "postgres-concurrency-test"
                    )
                )
            ).scalars().all() == []
    finally:
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            await session.execute(
                delete(ProviderRuntimeMetric).where(
                    ProviderRuntimeMetric.provider == "postgres-concurrency-test"
                )
            )
        await first.dispose()
        await second.dispose()
