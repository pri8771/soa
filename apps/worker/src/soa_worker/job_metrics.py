"""Queue metrics (JOB-008, FND-009).

Emits the queue's operational signals through the Telemetry facade:

- ``soa.jobs.claimed`` / ``completed`` / ``retried`` / ``dead_lettered``
  counters, dimensioned by job type (claim rate, completion, retry, and
  dead-letter rates fall out of these).
- ``soa.jobs.queue_depth`` gauge, dimensioned by status.
- ``soa.jobs.oldest_pending_age_seconds`` gauge.
- ``soa.jobs.seconds_since_last_claim`` gauge (no worker draining at all).

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


def _coerce_aware(value: object) -> datetime | None:
    """Aggregates bypass the UTCDateTime result processor: SQLite hands
    back the raw naive-UTC string for MAX() over a datetime column."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    assert isinstance(value, datetime)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
        oldest = _coerce_aware(
            (
                await session.execute(
                    select(func.min(Job.run_after)).where(Job.status == JobStatus.PENDING)
                )
            ).scalar()
        )
        age_seconds = 0.0
        if oldest is not None:
            age_seconds = max(0.0, (current - oldest).total_seconds())
        self._telemetry.set_gauge("soa.jobs.oldest_pending_age_seconds", age_seconds)

        # No worker draining the queue at all is a distinct, more urgent
        # failure than a merely-growing backlog (worker.absent alert): a
        # currently running job's heartbeat, or the completion time of the
        # most recently finished job, is the freshest evidence a worker
        # touched the queue. heartbeat_at is cleared on completion/failure,
        # so neither column alone captures both cases — take the newer.
        last_heartbeat = _coerce_aware(
            (await session.execute(select(func.max(Job.heartbeat_at)))).scalar()
        )
        last_finished = _coerce_aware(
            (await session.execute(select(func.max(Job.finished_at)))).scalar()
        )
        candidates = [value for value in (last_heartbeat, last_finished) if value is not None]
        if candidates:
            seconds_since_last_claim = max(0.0, (current - max(candidates)).total_seconds())
        elif oldest is not None:
            # Work is pending and no claim has ever happened: use the age
            # of the oldest pending job as the honest "since when" bound.
            seconds_since_last_claim = age_seconds
        else:
            seconds_since_last_claim = 0.0
        self._telemetry.set_gauge("soa.jobs.seconds_since_last_claim", seconds_since_last_claim)
