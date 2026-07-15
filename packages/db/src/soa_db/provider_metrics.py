"""Tenant-scoped provider health and routing metrics.

The routing path needs a signal shared by every worker replica.  In-memory
circuit state makes two replicas disagree and leaves the administration API
permanently reporting ``unknown``.  This table stores only bounded operational
facts (counts, latency, cost, confidence aggregates, and classified failures),
never document text, vendor responses, credential values, or raw errors.

Updates are one atomic upsert so concurrent worker replicas cannot lose
attempts.  Health is derived rather than asserted: three recent consecutive
failures open the circuit, one failure is degraded, a later success closes it,
and an open circuit becomes eligible for a half-open probe after a cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import BigInteger, CheckConstraint, Index, String, Table, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import UTCDateTime, utcnow, uuid7

PROVIDER_HEALTH_FAILURE_THRESHOLD = 3
PROVIDER_HEALTH_PROBE_COOLDOWN = timedelta(minutes=2)
PROVIDER_HEALTH_STALE_AFTER = timedelta(hours=1)
_QUALITY_SCALE = 1_000_000


class ProviderRuntimeMetric(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    Base,
):
    __tablename__ = "provider_runtime_metrics"

    capability: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    fallback_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(nullable=False, default=0)
    total_latency_ms: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    total_cost_cents: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    # Sum of per-result mean field confidence, scaled to an integer so atomic
    # cross-dialect updates do not depend on floating point behavior.
    quality_sum_micros: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    quality_sample_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    last_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # retryable|terminal only.  Raw exception text and vendor payloads are
    # deliberately excluded from this operational aggregate.
    last_failure_class: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "capability", "provider"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("success_count >= 0", name="success_count_nonnegative"),
        CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
        CheckConstraint("fallback_count >= 0", name="fallback_count_nonnegative"),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_nonnegative"),
        CheckConstraint("total_latency_ms >= 0", name="total_latency_nonnegative"),
        CheckConstraint("total_cost_cents >= 0", name="total_cost_nonnegative"),
        CheckConstraint("quality_sum_micros >= 0", name="quality_sum_nonnegative"),
        CheckConstraint("quality_sample_count >= 0", name="quality_samples_nonnegative"),
        CheckConstraint(
            "last_failure_class IS NULL OR last_failure_class IN ('retryable', 'terminal')",
            name="failure_class_valid",
        ),
        Index(
            "ix_provider_runtime_metrics_org_capability",
            "organization_id",
            "capability",
        ),
    )

    @property
    def mean_quality(self) -> float | None:
        if self.quality_sample_count <= 0:
            return None
        return self.quality_sum_micros / (self.quality_sample_count * _QUALITY_SCALE)


class ProviderRuntimeMetricRepository(ScopedRepository[ProviderRuntimeMetric]):
    model = ProviderRuntimeMetric

    async def list_all(self) -> list[ProviderRuntimeMetric]:
        stmt = self._scoped_select().order_by(
            ProviderRuntimeMetric.capability, ProviderRuntimeMetric.provider
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_for_capability(self, capability: str) -> list[ProviderRuntimeMetric]:
        stmt = (
            self._scoped_select()
            .where(ProviderRuntimeMetric.capability == capability)
            .order_by(ProviderRuntimeMetric.provider)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_for_provider(
        self, capability: str, provider: str
    ) -> ProviderRuntimeMetric | None:
        stmt = self._scoped_select().where(
            ProviderRuntimeMetric.capability == capability,
            ProviderRuntimeMetric.provider == provider,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True)
class ProviderOperationalSignals:
    health: dict[str, str]
    quality: dict[str, float]
    average_latency_ms: dict[str, float]
    average_cost_cents: dict[str, float]


def provider_health(
    metric: ProviderRuntimeMetric,
    *,
    now: datetime | None = None,
    failure_threshold: int = PROVIDER_HEALTH_FAILURE_THRESHOLD,
    probe_cooldown: timedelta = PROVIDER_HEALTH_PROBE_COOLDOWN,
    stale_after: timedelta = PROVIDER_HEALTH_STALE_AFTER,
) -> str:
    """Derive ``ok|degraded|unreachable|unknown`` from durable facts."""

    effective_now = now or utcnow()
    age = effective_now - metric.last_attempt_at
    if metric.consecutive_failures >= failure_threshold:
        # A bounded half-open probe prevents a provider from remaining
        # unreachable forever after the external service recovers.
        return "unreachable" if age < probe_cooldown else "degraded"
    if metric.consecutive_failures > 0:
        return "degraded"
    if age > stale_after:
        return "unknown"
    return "ok" if metric.success_count > 0 else "unknown"


async def provider_operational_signals(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    capability: str,
    now: datetime | None = None,
) -> ProviderOperationalSignals:
    metrics = await ProviderRuntimeMetricRepository(session, context).list_for_capability(
        capability
    )
    health: dict[str, str] = {}
    quality: dict[str, float] = {}
    latency: dict[str, float] = {}
    cost: dict[str, float] = {}
    for metric in metrics:
        health[metric.provider] = provider_health(metric, now=now)
        mean_quality = metric.mean_quality
        if mean_quality is not None:
            quality[metric.provider] = mean_quality
        if metric.success_count > 0:
            latency[metric.provider] = metric.total_latency_ms / metric.attempt_count
            cost[metric.provider] = metric.total_cost_cents / metric.attempt_count
    return ProviderOperationalSignals(
        health=health,
        quality=quality,
        average_latency_ms=latency,
        average_cost_cents=cost,
    )


async def record_provider_attempt(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    capability: str,
    provider: str,
    succeeded: bool,
    fallback: bool,
    latency_ms: int,
    cost_cents: int = 0,
    mean_quality: float | None = None,
    failure_class: str | None = None,
    now: datetime | None = None,
) -> ProviderRuntimeMetric:
    """Atomically record one provider call across every worker replica."""

    if not capability or len(capability) > 40:
        raise ValueError("provider metric capability must be 1..40 characters")
    if not provider or len(provider) > 100:
        raise ValueError("provider metric name must be 1..100 characters")
    if latency_ms < 0 or cost_cents < 0:
        raise ValueError("provider latency and cost cannot be negative")
    if mean_quality is not None and not 0 <= mean_quality <= 1:
        raise ValueError("provider quality must be within [0, 1]")
    if succeeded and failure_class is not None:
        raise ValueError("a successful provider attempt cannot have a failure class")
    if not succeeded and failure_class not in {"retryable", "terminal"}:
        raise ValueError("a failed provider attempt needs a classified failure")

    recorded_at = now or utcnow()
    quality_micros = round(mean_quality * _QUALITY_SCALE) if mean_quality is not None else 0
    table = cast(Table, ProviderRuntimeMetric.__table__)
    dialect = session.get_bind().dialect.name
    insert_factory = sqlite_insert if dialect == "sqlite" else postgresql_insert
    insert_stmt = insert_factory(table).values(
        id=uuid7(),
        organization_id=context.organization_id,
        capability=capability,
        provider=provider,
        attempt_count=1,
        success_count=1 if succeeded else 0,
        failure_count=0 if succeeded else 1,
        fallback_count=1 if fallback else 0,
        consecutive_failures=0 if succeeded else 1,
        total_latency_ms=latency_ms,
        total_cost_cents=cost_cents,
        quality_sum_micros=quality_micros,
        quality_sample_count=1 if mean_quality is not None else 0,
        last_attempt_at=recorded_at,
        last_success_at=recorded_at if succeeded else None,
        last_failure_at=None if succeeded else recorded_at,
        last_failure_class=None if succeeded else failure_class,
        created_at=recorded_at,
        updated_at=recorded_at,
    )
    update_values = {
        "attempt_count": table.c.attempt_count + 1,
        "success_count": table.c.success_count + (1 if succeeded else 0),
        "failure_count": table.c.failure_count + (0 if succeeded else 1),
        "fallback_count": table.c.fallback_count + (1 if fallback else 0),
        "consecutive_failures": 0 if succeeded else table.c.consecutive_failures + 1,
        "total_latency_ms": table.c.total_latency_ms + latency_ms,
        "total_cost_cents": table.c.total_cost_cents + cost_cents,
        "quality_sum_micros": table.c.quality_sum_micros + quality_micros,
        "quality_sample_count": table.c.quality_sample_count
        + (1 if mean_quality is not None else 0),
        "last_attempt_at": recorded_at,
        "last_success_at": recorded_at if succeeded else table.c.last_success_at,
        "last_failure_at": table.c.last_failure_at if succeeded else recorded_at,
        "last_failure_class": None if succeeded else failure_class,
        "updated_at": recorded_at,
    }
    await session.execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["organization_id", "capability", "provider"],
            set_=update_values,
        )
    )
    # Flush the upsert before reading through the ORM. ``populate_existing``
    # refreshes a row already present in this transaction without expiring
    # unrelated run/document objects (which would trigger async lazy loads).
    await session.flush()
    metric_stmt = (
        select(ProviderRuntimeMetric)
        .where(
            ProviderRuntimeMetric.organization_id == context.organization_id,
            ProviderRuntimeMetric.capability == capability,
            ProviderRuntimeMetric.provider == provider,
        )
        .execution_options(populate_existing=True)
    )
    metric = (await session.execute(metric_stmt)).scalar_one_or_none()
    if metric is None:  # defensive: the conflict target guarantees a row
        raise RuntimeError("provider metric upsert did not produce a row")
    return metric


__all__ = [
    "PROVIDER_HEALTH_FAILURE_THRESHOLD",
    "PROVIDER_HEALTH_PROBE_COOLDOWN",
    "PROVIDER_HEALTH_STALE_AFTER",
    "ProviderOperationalSignals",
    "ProviderRuntimeMetric",
    "ProviderRuntimeMetricRepository",
    "provider_health",
    "provider_operational_signals",
    "record_provider_attempt",
]
