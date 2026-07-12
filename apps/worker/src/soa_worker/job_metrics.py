"""Queue metrics (JOB-008, FND-009).

Emits the queue's operational signals through the Telemetry facade:

- ``soa.jobs.claimed`` / ``completed`` / ``retried`` / ``dead_lettered``
  counters, dimensioned by job type (claim rate, completion, retry, and
  dead-letter rates fall out of these).
- ``soa.jobs.queue_depth`` gauge, dimensioned by status.
- ``soa.jobs.oldest_pending_age_seconds`` gauge.

Approved dimensions are ``soa.job_type`` and ``soa.status`` ONLY. Tenant
IDs are deliberately never metric attributes: one time-series per tenant is
unbounded cardinality, and tenant-level questions belong to the audit trail
and job admin API, not the metrics pipeline. Job types come from code, but
the cap below bounds cardinality even if a bug floods the type namespace.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_config.telemetry import Telemetry
from soa_db.jobs import Job, JobStatus
from soa_db.types import utcnow

#: Distinct job-type attribute values before collapsing to "other".
MAX_JOB_TYPE_DIMENSIONS = 50


class JobMetrics:
    def __init__(self, telemetry: Telemetry) -> None:
        self._telemetry = telemetry
        self._seen_types: set[str] = set()

    def _type_dimension(self, job_type: str) -> str:
        if job_type in self._seen_types:
            return job_type
        if len(self._seen_types) >= MAX_JOB_TYPE_DIMENSIONS:
            return "other"
        self._seen_types.add(job_type)
        return job_type

    def record_claimed(self, jobs: list[Job]) -> None:
        for job in jobs:
            self._telemetry.add_to_counter(
                "soa.jobs.claimed", 1, {"soa.job_type": self._type_dimension(job.job_type)}
            )

    def record_completed(self, job: Job) -> None:
        self._telemetry.add_to_counter(
            "soa.jobs.completed", 1, {"soa.job_type": self._type_dimension(job.job_type)}
        )

    def record_retried(self, job: Job) -> None:
        self._telemetry.add_to_counter(
            "soa.jobs.retried", 1, {"soa.job_type": self._type_dimension(job.job_type)}
        )

    def record_dead_lettered(self, job: Job) -> None:
        self._telemetry.add_to_counter(
            "soa.jobs.dead_lettered", 1, {"soa.job_type": self._type_dimension(job.job_type)}
        )

    async def observe_queue(self, session: AsyncSession, *, now: datetime | None = None) -> None:
        """Snapshot depth per status and the oldest pending age (seconds).

        Called periodically by the worker loop; cross-tenant on purpose —
        the queue is one shared resource and its health is a platform
        signal.
        """
        current = now or utcnow()
        rows = (await session.execute(select(Job.status, func.count()).group_by(Job.status))).all()
        counts: dict[str, int] = {str(status): int(count) for status, count in rows}
        for status in JobStatus:
            self._telemetry.set_gauge(
                "soa.jobs.queue_depth",
                float(counts.get(str(status), 0)),
                {"soa.status": str(status)},
            )
        oldest_raw: object = (
            await session.execute(
                select(func.min(Job.run_after)).where(Job.status == JobStatus.PENDING)
            )
        ).scalar()
        age_seconds = 0.0
        if oldest_raw is not None:
            # Aggregates bypass the UTCDateTime result processor: SQLite
            # hands back the raw naive-UTC string.
            if isinstance(oldest_raw, str):
                oldest_raw = datetime.fromisoformat(oldest_raw)
            assert isinstance(oldest_raw, datetime)
            oldest = oldest_raw if oldest_raw.tzinfo else oldest_raw.replace(tzinfo=UTC)
            age_seconds = max(0.0, (current - oldest).total_seconds())
        self._telemetry.set_gauge("soa.jobs.oldest_pending_age_seconds", age_seconds)
