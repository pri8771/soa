"""Operational analytics model (ANA-001).

Tenant-scoped QUERIES over the operational tables (no shadow fact
tables to drift): volume, latency, backlog, SLA, and export health for
one organization and time window. Every metric ships with a
``MetricDefinition`` naming its numerator, denominator, and timezone —
a number nobody can define is a number nobody should chart.

Conventions:

- **timezone** — every timestamp is UTC and day buckets are UTC
  calendar days; the window is half-open ``[since, until)``.
- **tenant scope** — every query filters on the organization id (and
  RLS enforces the same boundary underneath in production).
- **bounded** — latency percentiles are computed over a capped sample
  (newest first); when the cap truncates, the result says so.
- **percentiles** — nearest-rank over the measured sample; an empty
  sample yields ``None``, never 0.
"""

import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.documents import Document, DocumentState
from soa_db.exports import DeliveryAttempt, ExportJob
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import ReviewTask, ReviewTaskState
from soa_db.runs import ProcessingRun
from soa_db.types import utcnow

#: Latency observations fetched per snapshot before capping (noted).
LATENCY_SAMPLE_CAP = 50_000

_ACTIVE_TASK_STATES = (ReviewTaskState.OPEN.value, ReviewTaskState.IN_PROGRESS.value)

#: Documents needing intervention rather than patience.
EXCEPTIONAL_DOCUMENT_STATES = tuple(
    state.value
    for state in (
        DocumentState.QUARANTINED,
        DocumentState.FAILED_RETRYABLE,
        DocumentState.FAILED_TERMINAL,
        DocumentState.REJECTED,
        DocumentState.CANCELLED,
    )
)

#: Document states that count as work still in the pipeline.
BACKLOG_DOCUMENT_STATES = tuple(
    state.value
    for state in (
        DocumentState.RECEIVED,
        DocumentState.VALIDATING_FILE,
        DocumentState.QUEUED,
        DocumentState.PREPROCESSING,
        DocumentState.CLASSIFYING,
        DocumentState.SPLITTING,
        DocumentState.EXTRACTING,
        DocumentState.NORMALIZING,
        DocumentState.VALIDATING_DATA,
        DocumentState.REVIEW_REQUIRED,
        DocumentState.APPROVED,
        DocumentState.EXPORTING,
        DocumentState.FAILED_RETRYABLE,
    )
)


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    description: str
    numerator: str
    denominator: str
    timezone: str = "UTC (day buckets are UTC calendar days; window is [since, until))"


DEFINITIONS: dict[str, MetricDefinition] = {
    definition.key: definition
    for definition in (
        MetricDefinition(
            key="volume.per_day",
            description="Documents received per UTC day, split by current state.",
            numerator="documents with received_at in the bucket",
            denominator="none — an absolute count",
        ),
        MetricDefinition(
            key="latency.run_ms",
            description="End-to-end processing-run latency in milliseconds.",
            numerator=(
                "total_latency_ms of runs FINISHED in the window (avg, and "
                "nearest-rank p50/p95 over the capped sample, newest first)"
            ),
            denominator="runs finished in the window (runs_measured)",
        ),
        MetricDefinition(
            key="backlog.documents_by_state",
            description="Documents currently in an in-pipeline state (snapshot, not windowed).",
            numerator="documents in the state NOW",
            denominator="none — an absolute count",
        ),
        MetricDefinition(
            key="backlog.review_tasks",
            description="Review tasks currently open or in progress (snapshot).",
            numerator="tasks in the state NOW",
            denominator="none — an absolute count",
        ),
        MetricDefinition(
            key="sla.overdue_now",
            description="Active review tasks past their SLA right now (snapshot).",
            numerator="open/in_progress tasks with sla_due_at < now",
            denominator="active_with_sla: open/in_progress tasks that have an SLA",
        ),
        MetricDefinition(
            key="sla.breached_completed",
            description="Completed review tasks that finished after their SLA.",
            numerator="tasks completed in the window with completed_at > sla_due_at",
            denominator=("completed_with_sla: tasks completed in the window that had an SLA"),
        ),
        MetricDefinition(
            key="exceptions.documents_by_state",
            description=(
                "Documents currently in an exceptional state — quarantined, "
                "failed, rejected, cancelled (snapshot, not windowed)."
            ),
            numerator="documents in the exceptional state NOW",
            denominator="none — an absolute count",
        ),
        MetricDefinition(
            key="exports.jobs_by_state",
            description="Export jobs created in the window, by current state.",
            numerator="export jobs with created_at in the window, per state",
            denominator="none — an absolute count",
        ),
        MetricDefinition(
            key="exports.attempt_success",
            description="Delivery attempt success rate in the window.",
            numerator="attempts started in the window with outcome 'delivered'",
            denominator="attempts started in the window (attempts_total)",
        ),
    )
}


@dataclass(frozen=True)
class LatencyStats:
    runs_measured: int
    sample_capped: bool
    avg_ms: float | None
    p50_ms: int | None
    p95_ms: int | None


def _nearest_rank(sorted_sample: list[int], percentile: float) -> int | None:
    if not sorted_sample:
        return None
    rank = max(1, math.ceil(percentile * len(sorted_sample)))
    return sorted_sample[min(rank, len(sorted_sample)) - 1]


async def operational_snapshot(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    since: datetime,
    until: datetime,
    stream_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """The ANA-001 read model: volume, latency, backlog, SLA, and
    export health for one tenant and window, with the definitions
    embedded so no consumer has to guess what a number means."""
    org = context.organization_id
    notes: list[str] = []
    if stream_id is not None:
        notes.append(
            "the stream filter applies to volume, latency, and document backlog — "
            "review-task, SLA, and export metrics are organization-wide"
        )

    def scoped_documents(stmt: Any) -> Any:
        stmt = stmt.where(Document.organization_id == org)
        if stream_id is not None:
            stmt = stmt.where(Document.stream_id == stream_id)
        return stmt

    # --- volume ---
    day = func.date(Document.received_at)
    volume_rows = await session.execute(
        scoped_documents(
            select(day.label("day"), Document.state, func.count())
            .where(Document.received_at >= since, Document.received_at < until)
            .group_by(day, Document.state)
            .order_by(day)
        )
    )
    per_day = [{"day": str(row.day), "state": row.state, "count": row[2]} for row in volume_rows]

    # --- latency ---
    latency_stmt = (
        select(ProcessingRun.total_latency_ms)
        .where(
            ProcessingRun.organization_id == org,
            ProcessingRun.finished_at.is_not(None),
            ProcessingRun.finished_at >= since,
            ProcessingRun.finished_at < until,
        )
        .order_by(ProcessingRun.finished_at.desc())
        .limit(LATENCY_SAMPLE_CAP + 1)
    )
    if stream_id is not None:
        latency_stmt = latency_stmt.join(Document, Document.id == ProcessingRun.document_id).where(
            Document.stream_id == stream_id, Document.organization_id == org
        )
    latencies = [row[0] for row in await session.execute(latency_stmt)]
    sample_capped = len(latencies) > LATENCY_SAMPLE_CAP
    latencies = sorted(latencies[:LATENCY_SAMPLE_CAP])
    if sample_capped:
        notes.append(
            f"latency percentiles use the newest {LATENCY_SAMPLE_CAP} runs — "
            "older runs in the window were not sampled"
        )
    latency = LatencyStats(
        runs_measured=len(latencies),
        sample_capped=sample_capped,
        avg_ms=round(sum(latencies) / len(latencies), 1) if latencies else None,
        p50_ms=_nearest_rank(latencies, 0.50),
        p95_ms=_nearest_rank(latencies, 0.95),
    )

    # --- backlog (snapshot, deliberately NOT windowed) ---
    backlog_rows = await session.execute(
        scoped_documents(
            select(Document.state, func.count())
            .where(Document.state.in_(BACKLOG_DOCUMENT_STATES))
            .group_by(Document.state)
        )
    )
    documents_by_state = {row.state: row[1] for row in backlog_rows}
    exception_rows = await session.execute(
        scoped_documents(
            select(Document.state, func.count())
            .where(Document.state.in_(EXCEPTIONAL_DOCUMENT_STATES))
            .group_by(Document.state)
        )
    )
    exceptions_by_state = {state: 0 for state in EXCEPTIONAL_DOCUMENT_STATES}
    exceptions_by_state.update({row.state: row[1] for row in exception_rows})
    task_rows = await session.execute(
        select(ReviewTask.state, func.count())
        .where(
            ReviewTask.organization_id == org,
            ReviewTask.state.in_(_ACTIVE_TASK_STATES),
        )
        .group_by(ReviewTask.state)
    )
    review_tasks: dict[str, int] = {state: 0 for state in _ACTIVE_TASK_STATES}
    review_tasks.update({row.state: row[1] for row in task_rows})
    review_tasks["blocking"] = (
        await session.execute(
            select(func.count()).where(
                ReviewTask.organization_id == org,
                ReviewTask.state.in_(_ACTIVE_TASK_STATES),
                ReviewTask.blocking.is_(True),
            )
        )
    ).scalar_one()

    # --- SLA ---
    now = utcnow()
    active_with_sla = (
        await session.execute(
            select(func.count()).where(
                ReviewTask.organization_id == org,
                ReviewTask.state.in_(_ACTIVE_TASK_STATES),
                ReviewTask.sla_due_at.is_not(None),
            )
        )
    ).scalar_one()
    overdue_now = (
        await session.execute(
            select(func.count()).where(
                ReviewTask.organization_id == org,
                ReviewTask.state.in_(_ACTIVE_TASK_STATES),
                ReviewTask.sla_due_at.is_not(None),
                ReviewTask.sla_due_at < now,
            )
        )
    ).scalar_one()
    completed_with_sla = (
        await session.execute(
            select(func.count()).where(
                ReviewTask.organization_id == org,
                ReviewTask.state == ReviewTaskState.COMPLETED.value,
                ReviewTask.completed_at.is_not(None),
                ReviewTask.completed_at >= since,
                ReviewTask.completed_at < until,
                ReviewTask.sla_due_at.is_not(None),
            )
        )
    ).scalar_one()
    breached_completed = (
        await session.execute(
            select(func.count()).where(
                ReviewTask.organization_id == org,
                ReviewTask.state == ReviewTaskState.COMPLETED.value,
                ReviewTask.completed_at.is_not(None),
                ReviewTask.completed_at >= since,
                ReviewTask.completed_at < until,
                ReviewTask.sla_due_at.is_not(None),
                ReviewTask.completed_at > ReviewTask.sla_due_at,
            )
        )
    ).scalar_one()

    # --- exports ---
    export_rows = await session.execute(
        select(ExportJob.state, func.count())
        .where(
            ExportJob.organization_id == org,
            ExportJob.created_at >= since,
            ExportJob.created_at < until,
        )
        .group_by(ExportJob.state)
    )
    jobs_by_state = {row.state: row[1] for row in export_rows}
    attempts_total = (
        await session.execute(
            select(func.count()).where(
                DeliveryAttempt.organization_id == org,
                DeliveryAttempt.started_at >= since,
                DeliveryAttempt.started_at < until,
            )
        )
    ).scalar_one()
    attempts_delivered = (
        await session.execute(
            select(func.count()).where(
                DeliveryAttempt.organization_id == org,
                DeliveryAttempt.started_at >= since,
                DeliveryAttempt.started_at < until,
                DeliveryAttempt.outcome == "delivered",
            )
        )
    ).scalar_one()

    return {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "timezone": "UTC",
        },
        "stream_id": str(stream_id) if stream_id is not None else None,
        "volume": {"per_day": per_day},
        "latency": asdict(latency),
        "backlog": {
            "documents_by_state": documents_by_state,
            "review_tasks": review_tasks,
        },
        "exceptions": {"documents_by_state": exceptions_by_state},
        "sla": {
            "overdue_now": overdue_now,
            "active_with_sla": active_with_sla,
            "breached_completed": breached_completed,
            "completed_with_sla": completed_with_sla,
        },
        "exports": {
            "jobs_by_state": jobs_by_state,
            "attempts_total": attempts_total,
            "attempts_delivered": attempts_delivered,
        },
        "notes": notes,
        "definitions": {key: asdict(value) for key, value in DEFINITIONS.items()},
    }


__all__ = [
    "BACKLOG_DOCUMENT_STATES",
    "DEFINITIONS",
    "EXCEPTIONAL_DOCUMENT_STATES",
    "LATENCY_SAMPLE_CAP",
    "LatencyStats",
    "MetricDefinition",
    "operational_snapshot",
]
