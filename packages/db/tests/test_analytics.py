"""Operational analytics tests (ANA-001): tenant scoping, the UTC
half-open window, documented definitions on every snapshot, honest
empty values (None, not 0, for unmeasured latency), and each metric
family computed from seeded fixtures."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.analytics import DEFINITIONS, operational_snapshot
from soa_db.documents import Document, SourceChannel, create_document
from soa_db.exports import create_export_job, record_delivery_attempt
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import claim_task, complete_task, route_document_to_review
from soa_db.runs import start_run
from soa_db.types import utcnow

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
OTHER_STREAM = uuid.UUID("88888888-8888-4888-8888-888888888888")
CONTEXT = OrganizationContext(organization_id=ORG)

WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 14, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/analytics.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_document(
    session: AsyncSession,
    *,
    context: OrganizationContext = CONTEXT,
    stream_id: uuid.UUID = STREAM,
    received_at: datetime | None = None,
) -> Document:
    document = await create_document(
        session,
        context,
        stream_id=stream_id,
        source_channel=SourceChannel.UPLOAD,
        original_filename="po.pdf",
        content_sha256=uuid.uuid4().hex * 2,
        size_bytes=100,
        content_type="application/pdf",
        actor_id="user:test",
    )
    if received_at is not None:
        document.received_at = received_at
        await session.flush()
    return document


async def seed_finished_run(
    session: AsyncSession, document: Document, *, latency_ms: int, finished_at: datetime
) -> None:
    run = await start_run(
        session,
        CONTEXT,
        document_id=document.id,
        input_sha256="a" * 64,
        stream_version_id=None,
        config_fingerprint=None,
        triggered_by="system:test",
    )
    run.total_latency_ms = latency_ms
    run.finished_at = finished_at
    await session.flush()


class TestSnapshot:
    async def test_volume_buckets_by_utc_day_and_respects_the_window(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            await seed_document(session, received_at=datetime(2026, 7, 2, 23, 59, tzinfo=UTC))
            await seed_document(session, received_at=datetime(2026, 7, 3, 0, 1, tzinfo=UTC))
            # Outside the window: excluded.
            await seed_document(session, received_at=datetime(2026, 6, 30, tzinfo=UTC))
            snapshot = await operational_snapshot(
                session, CONTEXT, since=WINDOW_START, until=WINDOW_END
            )
            days = {entry["day"]: entry["count"] for entry in snapshot["volume"]["per_day"]}
            assert days == {"2026-07-02": 1, "2026-07-03": 1}
            assert snapshot["window"]["timezone"] == "UTC"

    async def test_metrics_are_tenant_scoped(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            await seed_document(session, received_at=datetime(2026, 7, 2, tzinfo=UTC))
            await seed_document(
                session,
                context=OrganizationContext(organization_id=OTHER_ORG),
                received_at=datetime(2026, 7, 2, tzinfo=UTC),
            )
            snapshot = await operational_snapshot(
                session, CONTEXT, since=WINDOW_START, until=WINDOW_END
            )
            assert sum(e["count"] for e in snapshot["volume"]["per_day"]) == 1

    async def test_latency_percentiles_and_honest_empties(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            empty = await operational_snapshot(
                session, CONTEXT, since=WINDOW_START, until=WINDOW_END
            )
            assert empty["latency"]["runs_measured"] == 0
            assert empty["latency"]["avg_ms"] is None  # not a fake 0
            assert empty["latency"]["p95_ms"] is None

            document = await seed_document(session, received_at=datetime(2026, 7, 2, tzinfo=UTC))
            for latency in (100, 200, 300, 400, 1000):
                await seed_finished_run(
                    session,
                    document,
                    latency_ms=latency,
                    finished_at=datetime(2026, 7, 5, tzinfo=UTC),
                )
            snapshot = await operational_snapshot(
                session, CONTEXT, since=WINDOW_START, until=WINDOW_END
            )
            latency_stats = snapshot["latency"]
            assert latency_stats["runs_measured"] == 5
            assert latency_stats["avg_ms"] == 400.0
            assert latency_stats["p50_ms"] == 300  # nearest-rank over 5
            assert latency_stats["p95_ms"] == 1000
            assert latency_stats["sample_capped"] is False

    async def test_backlog_and_sla_snapshots(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            overdue_doc = await seed_document(session)
            fresh_doc = await seed_document(session)
            settled_doc = await seed_document(session)
            now = utcnow()
            reason = {
                "code": "low_confidence",
                "message": "below the gate",
                "field_key": "po_number",
            }
            await route_document_to_review(
                session,
                CONTEXT,
                document_id=overdue_doc.id,
                run_id=uuid.uuid4(),
                reasons=[reason],
                priority=10,
                sla_due_at=now - timedelta(hours=2),
            )
            await route_document_to_review(
                session,
                CONTEXT,
                document_id=fresh_doc.id,
                run_id=uuid.uuid4(),
                reasons=[reason],
                priority=10,
                sla_due_at=now + timedelta(hours=8),
            )
            late = await route_document_to_review(
                session,
                CONTEXT,
                document_id=settled_doc.id,
                run_id=uuid.uuid4(),
                reasons=[reason],
                priority=10,
                sla_due_at=now - timedelta(days=1),
            )
            await claim_task(session, CONTEXT, task=late, user_id="user:test")
            await complete_task(
                session, CONTEXT, task=late, outcome="approved", actor_id="user:test"
            )

            snapshot = await operational_snapshot(
                session,
                CONTEXT,
                since=now - timedelta(days=1),
                until=now + timedelta(days=1),
            )
            assert snapshot["backlog"]["review_tasks"] == {"open": 2, "in_progress": 0}
            assert snapshot["backlog"]["documents_by_state"]["received"] == 3
            assert snapshot["sla"]["overdue_now"] == 1
            assert snapshot["sla"]["active_with_sla"] == 2
            # The completed task finished after its SLA: a breach on record.
            assert snapshot["sla"]["breached_completed"] == 1
            assert snapshot["sla"]["completed_with_sla"] == 1

    async def test_export_jobs_and_attempt_outcomes(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            document = await seed_document(session)
            job = await create_export_job(
                session,
                CONTEXT,
                document_id=document.id,
                run_id=uuid.uuid4(),
                canonical_payload_id=uuid.uuid4(),
                integration_id=uuid.uuid4(),
                mapping_version_id=uuid.uuid4(),
                actor_id="system:test",
            )
            await record_delivery_attempt(session, CONTEXT, job=job, outcome="delivered")
            await record_delivery_attempt(
                session, CONTEXT, job=job, outcome="retryable_error", safe_error="timeout"
            )
            now = utcnow()
            snapshot = await operational_snapshot(
                session,
                CONTEXT,
                since=now - timedelta(hours=1),
                until=now + timedelta(hours=1),
            )
            assert snapshot["exports"]["jobs_by_state"] == {"pending": 1}
            assert snapshot["exports"]["attempts_total"] == 2
            assert snapshot["exports"]["attempts_delivered"] == 1

    async def test_the_stream_filter_applies_where_meaningful_and_says_so(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            await seed_document(session, received_at=datetime(2026, 7, 2, tzinfo=UTC))
            await seed_document(
                session,
                stream_id=OTHER_STREAM,
                received_at=datetime(2026, 7, 2, tzinfo=UTC),
            )
            snapshot = await operational_snapshot(
                session,
                CONTEXT,
                since=WINDOW_START,
                until=WINDOW_END,
                stream_id=STREAM,
            )
            assert sum(e["count"] for e in snapshot["volume"]["per_day"]) == 1
            assert any("organization-wide" in note for note in snapshot["notes"])


class TestDefinitions:
    def test_every_metric_has_numerator_denominator_and_timezone(self) -> None:
        assert set(DEFINITIONS) == {
            "volume.per_day",
            "latency.run_ms",
            "backlog.documents_by_state",
            "backlog.review_tasks",
            "sla.overdue_now",
            "sla.breached_completed",
            "exports.jobs_by_state",
            "exports.attempt_success",
        }
        for definition in DEFINITIONS.values():
            assert definition.description
            assert definition.numerator
            assert definition.denominator
            assert "UTC" in definition.timezone

    async def test_definitions_travel_on_the_snapshot(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            snapshot = await operational_snapshot(
                session, CONTEXT, since=WINDOW_START, until=WINDOW_END
            )
            assert snapshot["definitions"]["sla.overdue_now"]["numerator"]
