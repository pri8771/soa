"""Quality analytics tests (ANA-002): correction rates with sample
sizes, line-cell rates, STP, calibration cohorts, and the honesty
guarantees — unmeasurable rates are None, false auto-approval is
declared unavailable, and every rate names its proxy nature."""

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.analytics_quality import QUALITY_DEFINITIONS, quality_snapshot
from soa_db.corrections import record_correction
from soa_db.documents import Document, DocumentState, SourceChannel, create_document
from soa_db.extracted_fields import create_extracted_field
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import ReviewTask, claim_task, complete_task, route_document_to_review
from soa_db.runs import start_run
from soa_db.types import utcnow

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONTEXT = OrganizationContext(organization_id=ORG)

REASON = {"code": "low_confidence", "message": "below the gate", "field_key": "po_number"}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/quality.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_reviewed_run(
    session: AsyncSession,
    *,
    fields: dict[str, float],
    corrected: tuple[str, ...] = (),
    line_cells: dict[tuple[str, int], tuple[float, bool]] | None = None,
) -> tuple[Document, ReviewTask]:
    """A document with one run, extracted header fields at the given
    confidences, optional line cells (confidence, corrected?), routed to
    review, claimed, corrected, and completed."""
    document = await create_document(
        session,
        CONTEXT,
        stream_id=STREAM,
        source_channel=SourceChannel.UPLOAD,
        original_filename="po.pdf",
        content_sha256=uuid.uuid4().hex * 2,
        size_bytes=100,
        content_type="application/pdf",
        actor_id="user:test",
    )
    run = await start_run(
        session,
        CONTEXT,
        document_id=document.id,
        input_sha256="a" * 64,
        stream_version_id=None,
        config_fingerprint=None,
        triggered_by="system:test",
    )
    for field_key, confidence in fields.items():
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run.id,
            field_key=field_key,
            raw_value="value",
            confidence=confidence,
            provider="mock",
            row_index=None,
            normalized_value="value",
        )
    for (field_key, row_index), (confidence, _will_correct) in (line_cells or {}).items():
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run.id,
            field_key=field_key,
            raw_value="cell",
            confidence=confidence,
            provider="mock",
            row_index=row_index,
            normalized_value="cell",
        )
    task = await route_document_to_review(
        session,
        CONTEXT,
        document_id=document.id,
        run_id=run.id,
        reasons=[REASON],
        priority=10,
    )
    await claim_task(session, CONTEXT, task=task, user_id="user:reviewer")
    for field_key in corrected:
        await record_correction(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run.id,
            task_id=task.id,
            field_key=field_key,
            row_index=None,
            previous_raw_value="value",
            corrected_raw_value="fixed",
            corrected_normalized_value="fixed",
            normalization_error=None,
            reason="wrong",
            evidence_selection=None,
            corrected_by="user:reviewer",
            task_version=task.version,
        )
    for (field_key, row_index), (_confidence, will_correct) in (line_cells or {}).items():
        if will_correct:
            await record_correction(
                session,
                CONTEXT,
                document_id=document.id,
                run_id=run.id,
                task_id=task.id,
                field_key=field_key,
                row_index=row_index,
                previous_raw_value="cell",
                corrected_raw_value="fixed",
                corrected_normalized_value="fixed",
                normalization_error=None,
                reason="wrong",
                evidence_selection=None,
                corrected_by="user:reviewer",
                task_version=task.version,
            )
    await complete_task(session, CONTEXT, task=task, outcome="approved", actor_id="user:reviewer")
    return document, task


def window() -> tuple[datetime, datetime]:
    now = utcnow()
    return now - timedelta(hours=1), now + timedelta(hours=1)


class TestCorrectionRates:
    async def test_field_rates_carry_numerator_denominator_and_rate(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            await seed_reviewed_run(
                session, fields={"po_number": 0.6, "total_amount": 0.97}, corrected=("po_number",)
            )
            await seed_reviewed_run(
                session, fields={"po_number": 0.7, "total_amount": 0.96}, corrected=()
            )
            since, until = window()
            snapshot = await quality_snapshot(session, CONTEXT, since=since, until=until)
            assert snapshot["reviewed"]["tasks_completed"] == 2
            by_key = {entry["field_key"]: entry for entry in snapshot["field_corrections"]}
            assert by_key["po_number"] == {
                "field_key": "po_number",
                "present_runs": 2,
                "corrected_runs": 1,
                "correction_rate": 0.5,
            }
            assert by_key["total_amount"]["correction_rate"] == 0.0

    async def test_line_cells_are_counted_separately(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            await seed_reviewed_run(
                session,
                fields={"po_number": 0.9},
                line_cells={
                    ("lines.sku", 0): (0.9, True),
                    ("lines.quantity", 0): (0.9, False),
                    ("lines.sku", 1): (0.9, False),
                },
            )
            since, until = window()
            snapshot = await quality_snapshot(session, CONTEXT, since=since, until=until)
            lines = snapshot["line_corrections"]
            assert lines == {
                "cells_present": 3,
                "cells_corrected": 1,
                "correction_rate": 0.3333,
            }

    async def test_calibration_buckets_by_confidence(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            await seed_reviewed_run(
                session,
                fields={"po_number": 0.6, "customer_name": 0.99, "currency": 0.97},
                corrected=("po_number",),
            )
            since, until = window()
            snapshot = await quality_snapshot(session, CONTEXT, since=since, until=until)
            cohorts = {c["confidence_range"]: c for c in snapshot["calibration"]["cohorts"]}
            mid = cohorts["[0.5, 0.8)"]
            assert (mid["fields_reviewed"], mid["corrected"]) == (1, 1)
            assert mid["correction_rate"] == 1.0
            top = cohorts["[0.95, 1.0]"]
            assert (top["fields_reviewed"], top["corrected"]) == (2, 0)
            assert top["correction_rate"] == 0.0


class TestHonesty:
    async def test_empty_windows_yield_none_rates_not_zero(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            since, until = window()
            snapshot = await quality_snapshot(session, CONTEXT, since=since, until=until)
            assert snapshot["field_corrections"] == []
            assert snapshot["line_corrections"]["correction_rate"] is None
            assert snapshot["stp"]["stp_rate"] is None
            for cohort in snapshot["calibration"]["cohorts"]:
                assert cohort["correction_rate"] is None

    async def test_false_auto_approval_is_declared_unavailable(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            since, until = window()
            snapshot = await quality_snapshot(session, CONTEXT, since=since, until=until)
            assert snapshot["false_auto_approval"]["available"] is False
            assert "gold" in snapshot["false_auto_approval"]["reason"]
            assert snapshot["ground_truth"]["gold_documents"] == 0
            assert "PROXIES" in snapshot["ground_truth"]["note"]

    def test_every_definition_names_its_semantics(self) -> None:
        for definition in QUALITY_DEFINITIONS.values():
            assert definition.numerator and definition.denominator
            assert "UTC" in definition.timezone
        assert "PROXY" in QUALITY_DEFINITIONS["quality.field_correction_rate"].description


class TestStp:
    async def test_stp_counts_settled_documents_without_review(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            # One reviewed document, one straight-through, one unsettled.
            reviewed_doc, _ = await seed_reviewed_run(session, fields={"po_number": 0.9})
            auto = await create_document(
                session,
                CONTEXT,
                stream_id=STREAM,
                source_channel=SourceChannel.UPLOAD,
                original_filename="auto.pdf",
                content_sha256=uuid.uuid4().hex * 2,
                size_bytes=100,
                content_type="application/pdf",
                actor_id="user:test",
            )
            await create_document(
                session,
                CONTEXT,
                stream_id=STREAM,
                source_channel=SourceChannel.UPLOAD,
                original_filename="pending.pdf",
                content_sha256=uuid.uuid4().hex * 2,
                size_bytes=100,
                content_type="application/pdf",
                actor_id="user:test",
            )
            # Walk the two settled documents to a settled state.
            for document in (reviewed_doc, auto):
                for state in (
                    DocumentState.VALIDATING_FILE,
                    DocumentState.QUEUED,
                    DocumentState.PREPROCESSING,
                    DocumentState.CLASSIFYING,
                    DocumentState.SPLITTING,
                    DocumentState.EXTRACTING,
                    DocumentState.NORMALIZING,
                    DocumentState.VALIDATING_DATA,
                    DocumentState.APPROVED,
                ):
                    document.state = state.value
                    await session.flush()
            since, until = window()
            snapshot = await quality_snapshot(session, CONTEXT, since=since, until=until)
            assert snapshot["stp"]["settled_documents"] == 2
            assert snapshot["stp"]["straight_through"] == 1
            assert snapshot["stp"]["stp_rate"] == 0.5
