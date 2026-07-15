"""Duplicate PO business validation tests (CAT-013): discovery over
effective (correction-aware, latest-run) values with customer/date
comparison, and the pure policy assessment — warn/block/allow per
stream, rejected candidates never flag, block overridable only with a
written reason."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.corrections import record_correction
from soa_db.documents import Document, SourceChannel, create_document
from soa_db.duplicate_po import (
    DuplicateOverride,
    DuplicateOverrideError,
    DuplicatePoPolicy,
    DuplicatePoPolicyValidationError,
    PoDuplicateCandidate,
    assess_po_duplicates,
    find_po_duplicates,
    get_po_duplicate_policy,
    validate_po_duplicate_policy,
)
from soa_db.extracted_fields import create_extracted_field
from soa_db.repository import OrganizationContext
from soa_db.runs import start_run

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
OTHER_STREAM = uuid.UUID("88888888-8888-4888-8888-888888888888")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/duplicate-po.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed(
    session: AsyncSession,
    *,
    po: str | None,
    customer: str | None = "Acme GmbH",
    order_date: str | None = "2026-07-01",
    stream_id: uuid.UUID = STREAM,
    filename: str = "po.pdf",
) -> tuple[Document, uuid.UUID]:
    document = await create_document(
        session,
        CONTEXT,
        stream_id=stream_id,
        source_channel=SourceChannel.UPLOAD,
        original_filename=filename,
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
    for key, value in (
        ("po_number", po),
        ("customer_name", customer),
        ("order_date", order_date),
    ):
        if value is not None:
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=document.id,
                run_id=run.id,
                field_key=key,
                raw_value=value,
                confidence=0.9,
                provider="mock",
                row_index=None,
                normalized_value=value,
            )
    return document, run.id


class TestDiscovery:
    async def test_same_po_and_customer_in_the_stream_is_a_candidate(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            original, _ = await seed(session, po="PO-1001")
            elsewhere, _ = await seed(session, po="PO-1001", stream_id=OTHER_STREAM)
            incoming, _ = await seed(session, po="po 001001", filename="retyped.pdf")
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=incoming.id,
                po_number="po 001001",  # normalizes to PO1001
                customer="ACME GmbH",
                order_date="2026-07-01",
            )
            assert [c.document_id for c in search.candidates] == [original.id]
            (candidate,) = search.candidates
            assert candidate.customer_matched is True
            assert candidate.order_date_matched is True
            assert elsewhere.id not in {c.document_id for c in search.candidates}

    async def test_a_different_customer_is_excluded_with_a_note(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            rival, _ = await seed(session, po="PO-2002", customer="Rival Industries")
            incoming, _ = await seed(session, po="PO-2002")
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=incoming.id,
                po_number="PO-2002",
                customer="Acme GmbH",
            )
            assert search.candidates == ()
            assert any(
                "different customer" in note and str(rival.id) in note for note in search.notes
            )

    async def test_an_incomparable_customer_stays_with_the_uncertainty_recorded(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            nameless, _ = await seed(session, po="PO-3003", customer=None)
            incoming, _ = await seed(session, po="PO-3003")
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=incoming.id,
                po_number="PO-3003",
                customer="Acme GmbH",
            )
            (candidate,) = search.candidates
            assert candidate.document_id == nameless.id
            assert candidate.customer_matched is None
            assert any("could not be compared" in note for note in search.notes)

    async def test_corrections_override_the_extraction_both_ways(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            # Extracted PO-X, corrected to PO-4004: becomes a candidate.
            corrected_in, run_in = await seed(session, po="PO-X")
            await record_correction(
                session,
                CONTEXT,
                document_id=corrected_in.id,
                run_id=run_in,
                task_id=uuid.uuid4(),
                field_key="po_number",
                row_index=None,
                previous_raw_value="PO-X",
                corrected_raw_value="PO-4004",
                corrected_normalized_value="PO-4004",
                normalization_error=None,
                reason="misread",
                evidence_selection=None,
                corrected_by="user:reviewer",
                task_version=1,
            )
            # Extracted PO-4004, corrected away: no longer a candidate.
            corrected_out, run_out = await seed(session, po="PO-4004")
            await record_correction(
                session,
                CONTEXT,
                document_id=corrected_out.id,
                run_id=run_out,
                task_id=uuid.uuid4(),
                field_key="po_number",
                row_index=None,
                previous_raw_value="PO-4004",
                corrected_raw_value="PO-9999",
                corrected_normalized_value="PO-9999",
                normalization_error=None,
                reason="misread",
                evidence_selection=None,
                corrected_by="user:reviewer",
                task_version=1,
            )
            incoming, _ = await seed(session, po="PO-4004")
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=incoming.id,
                po_number="PO-4004",
                customer="Acme GmbH",
            )
            assert [c.document_id for c in search.candidates] == [corrected_in.id]

    async def test_the_latest_run_wins_after_reprocessing(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            reprocessed, _ = await seed(session, po="PO-5005")
            second_run = await start_run(
                session,
                CONTEXT,
                document_id=reprocessed.id,
                input_sha256="b" * 64,
                stream_version_id=None,
                config_fingerprint=None,
                triggered_by="system:test",
                reason="reprocess",
            )
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=reprocessed.id,
                run_id=second_run.id,
                field_key="po_number",
                raw_value="PO-OTHER",
                confidence=0.9,
                provider="mock",
                row_index=None,
                normalized_value="PO-OTHER",
            )
            incoming, _ = await seed(session, po="PO-5005")
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=incoming.id,
                po_number="PO-5005",
                customer="Acme GmbH",
            )
            assert search.candidates == ()

    async def test_a_different_order_date_is_flagged_as_a_possible_revision(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            original, _ = await seed(session, po="PO-6006", order_date="2026-06-01")
            incoming, _ = await seed(session, po="PO-6006", order_date="2026-07-01")
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=incoming.id,
                po_number="PO-6006",
                customer="Acme GmbH",
                order_date="2026-07-01",
            )
            (candidate,) = search.candidates
            assert candidate.document_id == original.id
            assert candidate.order_date_matched is False

    async def test_a_missing_po_number_skips_the_check_honestly(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            search = await find_po_duplicates(
                session,
                CONTEXT,
                stream_id=STREAM,
                exclude_document_id=uuid.uuid4(),
                po_number=None,
            )
            assert search.candidates == ()
            assert any("was skipped" in note for note in search.notes)


def candidate(
    state: str = "review_required", date_matched: bool | None = True
) -> PoDuplicateCandidate:
    return PoDuplicateCandidate(
        document_id=uuid.uuid4(),
        document_state=state,
        received_at=datetime(2026, 7, 1, tzinfo=UTC),
        po_number="PO-1001",
        customer_matched=True,
        order_date_matched=date_matched,
    )


class TestPolicyAssessment:
    def test_warn_surfaces_a_warning_naming_the_candidates(self) -> None:
        duplicate = candidate()
        result = assess_po_duplicates([duplicate], policy=DuplicatePoPolicy.WARN)
        (finding,) = result.findings
        assert (finding.code, finding.severity) == ("duplicate_po", "warning")
        assert str(duplicate.document_id) in finding.message
        assert finding.to_reason()["rule_key"] == "catalog.duplicate_po"

    def test_block_is_an_error_that_names_the_override_path(self) -> None:
        result = assess_po_duplicates([candidate()], policy=DuplicatePoPolicy.BLOCK)
        (finding,) = result.findings
        assert (finding.code, finding.severity) == ("duplicate_po", "error")
        assert "override" in finding.message

    def test_an_override_downgrades_the_block_with_the_reason_on_record(self) -> None:
        override = DuplicateOverride(
            actor_id="user:supervisor", reason="revision RV-2 of the June order"
        )
        result = assess_po_duplicates(
            [candidate(date_matched=False)],
            policy=DuplicatePoPolicy.BLOCK,
            override=override,
        )
        (finding,) = result.findings
        assert (finding.code, finding.severity) == ("duplicate_po_overridden", "warning")
        assert "user:supervisor" in finding.message
        assert "revision RV-2" in finding.message
        assert "possibly a revision" in finding.message
        assert override.to_record() == {
            "kind": "duplicate_po_override",
            "actor_id": "user:supervisor",
            "reason": "revision RV-2 of the June order",
        }

    def test_an_override_without_a_written_reason_is_refused(self) -> None:
        with pytest.raises(DuplicateOverrideError, match="written reason"):
            DuplicateOverride(actor_id="user:supervisor", reason="   ")

    def test_allow_records_a_note_and_no_finding(self) -> None:
        result = assess_po_duplicates([candidate()], policy=DuplicatePoPolicy.ALLOW)
        assert result.findings == ()
        assert any("policy allows" in note for note in result.notes)

    def test_rejected_candidates_never_warn_or_block(self) -> None:
        result = assess_po_duplicates([candidate(state="rejected")], policy=DuplicatePoPolicy.BLOCK)
        assert result.findings == ()
        assert any("resubmission" in note for note in result.notes)

    def test_the_stream_policy_parses_and_defaults_safely(self) -> None:
        assert get_po_duplicate_policy({"business_duplicate_policy": "block"}) == (
            DuplicatePoPolicy.BLOCK
        )
        assert get_po_duplicate_policy({"business_duplicate_policy": "nonsense"}) == (
            DuplicatePoPolicy.WARN
        )
        assert get_po_duplicate_policy({}) == DuplicatePoPolicy.WARN

    @pytest.mark.parametrize("value", ["warn", "block", "allow"])
    def test_strict_write_validation_accepts_documented_values(self, value: str) -> None:
        assert validate_po_duplicate_policy({"business_duplicate_policy": value}) is (
            DuplicatePoPolicy(value)
        )

    @pytest.mark.parametrize("value", ["nonsense", "WARN", "", None, 1])
    def test_strict_write_validation_rejects_invalid_values(self, value: object) -> None:
        with pytest.raises(DuplicatePoPolicyValidationError, match="warn, block, allow"):
            validate_po_duplicate_policy({"business_duplicate_policy": value})
