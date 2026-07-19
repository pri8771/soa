"""Queue metrics tests (JOB-008): approved dimensions only, bounded
cardinality, and honest depth/age gauges."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from soa_config.telemetry import configure_telemetry
from soa_db import Base, DatabaseSessions, create_database_engine, utcnow
from soa_db.jobs import Job, claim_next_jobs, enqueue_job, mark_succeeded
from soa_worker.job_metrics import MAX_JOB_TYPE_DIMENSIONS, JobMetrics


def make_harness() -> tuple[JobMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    telemetry = configure_telemetry(
        service_name="soa-worker-test",
        environment="test",
        profile="console",
        metric_reader=reader,
    )
    return JobMetrics(telemetry), reader


def collect_points(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, Any], float]]]:
    data = reader.get_metrics_data()
    points: dict[str, list[tuple[dict[str, Any], float]]] = {}
    if data is None:
        return points
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    points.setdefault(metric.name, []).append(
                        (dict(point.attributes), float(point.value))
                    )
    return points


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/metrics.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def make_job(job_type: str = "document.extract") -> Job:
    return Job(job_type=job_type, payload={}, organization_id=None)


def test_counters_use_only_approved_dimensions() -> None:
    metrics, reader = make_harness()
    job = make_job()
    job.organization_id = None
    metrics.record_claimed([job])
    metrics.record_completed(job)
    metrics.record_retried(job)
    metrics.record_dead_lettered(job)

    points = collect_points(reader)
    for name in (
        "soa.jobs.claimed",
        "soa.jobs.completed",
        "soa.jobs.retried",
        "soa.jobs.dead_lettered",
    ):
        assert name in points
        for attributes, value in points[name]:
            assert value == 1
            assert set(attributes) == {"soa.job_type"}, (
                "tenant IDs and other unapproved dimensions must never reach metrics"
            )


def test_job_type_cardinality_is_bounded() -> None:
    metrics, reader = make_harness()
    for index in range(MAX_JOB_TYPE_DIMENSIONS + 25):
        metrics.record_completed(make_job(job_type=f"type-{index}"))

    points = collect_points(reader)
    labels = {attributes["soa.job_type"] for attributes, _ in points["soa.jobs.completed"]}
    assert len(labels) == MAX_JOB_TYPE_DIMENSIONS + 1
    assert "other" in labels
    overflow = sum(
        value
        for attributes, value in points["soa.jobs.completed"]
        if attributes["soa.job_type"] == "other"
    )
    assert overflow == 25


async def test_queue_gauges_report_depth_and_oldest_age(sessions: DatabaseSessions) -> None:
    metrics, reader = make_harness()
    moment = utcnow()
    async with sessions.session_scope() as session:
        await enqueue_job(
            session, job_type="a", payload={}, run_after=moment - timedelta(minutes=10)
        )
        await enqueue_job(session, job_type="b", payload={}, run_after=moment)

    async with sessions.session_scope() as session:
        await metrics.observe_queue(session, now=moment)

    points = collect_points(reader)
    depth = {
        attributes["soa.status"]: value for attributes, value in points["soa.jobs.queue_depth"]
    }
    assert depth["pending"] == 2
    assert depth["running"] == 0
    (age_point,) = points["soa.jobs.oldest_pending_age_seconds"]
    assert age_point[0] == {}
    assert age_point[1] == pytest.approx(600, abs=1)


async def test_oldest_age_is_zero_for_an_empty_queue(sessions: DatabaseSessions) -> None:
    metrics, reader = make_harness()
    async with sessions.session_scope() as session:
        await metrics.observe_queue(session)
    points = collect_points(reader)
    (age_point,) = points["soa.jobs.oldest_pending_age_seconds"]
    assert age_point[1] == 0.0


async def test_seconds_since_last_claim_is_zero_for_an_empty_queue(
    sessions: DatabaseSessions,
) -> None:
    metrics, reader = make_harness()
    async with sessions.session_scope() as session:
        await metrics.observe_queue(session)
    points = collect_points(reader)
    (point,) = points["soa.jobs.seconds_since_last_claim"]
    assert point[1] == 0.0


async def test_seconds_since_last_claim_falls_back_to_oldest_pending_age(
    sessions: DatabaseSessions,
) -> None:
    """No job has ever been claimed while work is pending: the signal must
    still surface a "since when" bound instead of reporting a false zero."""
    metrics, reader = make_harness()
    moment = utcnow()
    async with sessions.session_scope() as session:
        await enqueue_job(
            session, job_type="a", payload={}, run_after=moment - timedelta(minutes=10)
        )

    async with sessions.session_scope() as session:
        await metrics.observe_queue(session, now=moment)

    points = collect_points(reader)
    (point,) = points["soa.jobs.seconds_since_last_claim"]
    assert point[1] == pytest.approx(600, abs=1)


async def test_seconds_since_last_claim_reflects_a_running_jobs_heartbeat(
    sessions: DatabaseSessions,
) -> None:
    metrics, reader = make_harness()
    moment = utcnow()
    async with sessions.session_scope() as session:
        await enqueue_job(session, job_type="a", payload={}, run_after=moment - timedelta(hours=1))
        await claim_next_jobs(session, worker_id="w1", now=moment - timedelta(minutes=5))

    async with sessions.session_scope() as session:
        await metrics.observe_queue(session, now=moment)

    points = collect_points(reader)
    (point,) = points["soa.jobs.seconds_since_last_claim"]
    assert point[1] == pytest.approx(300, abs=1)


async def test_seconds_since_last_claim_reflects_a_recently_finished_job(
    sessions: DatabaseSessions,
) -> None:
    metrics, reader = make_harness()
    moment = utcnow()
    async with sessions.session_scope() as session:
        await enqueue_job(session, job_type="a", payload={}, run_after=moment - timedelta(hours=1))
        (claimed,) = await claim_next_jobs(
            session, worker_id="w1", now=moment - timedelta(minutes=2)
        )
        mark_succeeded(claimed, worker_id="w1", now=moment - timedelta(minutes=1))

    async with sessions.session_scope() as session:
        await metrics.observe_queue(session, now=moment)

    points = collect_points(reader)
    (point,) = points["soa.jobs.seconds_since_last_claim"]
    assert point[1] == pytest.approx(60, abs=1)
