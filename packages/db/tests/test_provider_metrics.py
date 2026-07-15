"""Durable cross-replica provider health facts."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.provider_metrics import (
    PROVIDER_HEALTH_PROBE_COOLDOWN,
    ProviderRuntimeMetricRepository,
    provider_health,
    provider_operational_signals,
    record_provider_attempt,
)
from soa_db.repository import OrganizationContext

ORG = OrganizationContext(uuid.UUID("11111111-1111-4111-8111-111111111111"))
OTHER_ORG = OrganizationContext(uuid.UUID("22222222-2222-4222-8222-222222222222"))
NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/provider-metrics.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_attempts_upsert_without_losing_health_cost_or_quality(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        metric = await record_provider_attempt(
            session,
            ORG,
            capability="field_extraction",
            provider="hosted-test",
            succeeded=True,
            fallback=False,
            latency_ms=120,
            cost_cents=7,
            mean_quality=0.8,
            now=NOW,
        )
        assert provider_health(metric, now=NOW) == "ok"
        metric = await record_provider_attempt(
            session,
            ORG,
            capability="field_extraction",
            provider="hosted-test",
            succeeded=False,
            fallback=True,
            latency_ms=30,
            failure_class="retryable",
            now=NOW + timedelta(seconds=1),
        )
        assert metric.attempt_count == 2
        assert metric.success_count == 1
        assert metric.failure_count == 1
        assert metric.fallback_count == 1
        assert metric.total_latency_ms == 150
        assert metric.total_cost_cents == 7
        assert metric.mean_quality == pytest.approx(0.8)
        assert provider_health(metric, now=NOW + timedelta(seconds=1)) == "degraded"


async def test_circuit_opens_after_three_failures_then_allows_a_probe(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        metric = None
        for offset in range(3):
            metric = await record_provider_attempt(
                session,
                ORG,
                capability="field_extraction",
                provider="outage-test",
                succeeded=False,
                fallback=offset > 0,
                latency_ms=10,
                failure_class="retryable",
                now=NOW + timedelta(seconds=offset),
            )
        assert metric is not None
        last = NOW + timedelta(seconds=2)
        assert provider_health(metric, now=last) == "unreachable"
        assert provider_health(metric, now=last + PROVIDER_HEALTH_PROBE_COOLDOWN) == "degraded"


async def test_signals_are_tenant_scoped_and_unknown_is_not_fabricated(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        await record_provider_attempt(
            session,
            ORG,
            capability="field_extraction",
            provider="local-test",
            succeeded=True,
            fallback=False,
            latency_ms=25,
            mean_quality=0.9,
            now=NOW,
        )
        signals = await provider_operational_signals(
            session,
            ORG,
            capability="field_extraction",
            now=NOW,
        )
        assert signals.health == {"local-test": "ok"}
        assert signals.quality == {"local-test": pytest.approx(0.9)}
        assert signals.average_latency_ms == {"local-test": 25.0}
        assert (
            await ProviderRuntimeMetricRepository(session, OTHER_ORG).list_for_capability(
                "field_extraction"
            )
            == []
        )


async def test_invalid_failure_facts_are_rejected_before_persistence(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="classified failure"):
            await record_provider_attempt(
                session,
                ORG,
                capability="field_extraction",
                provider="bad-test",
                succeeded=False,
                fallback=False,
                latency_ms=1,
            )
