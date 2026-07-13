"""Extracted-field and evidence model tests (PRC-007): serialization,
coordinate honesty, row identity, candidate promotion, summaries."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.extracted_fields import (
    Candidate,
    Evidence,
    EvidenceCertainty,
    ExtractedFieldRepository,
    ValidationStatus,
    confidence_summary,
    create_extracted_field,
    promote_candidate,
    validation_summary,
)
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN = uuid.UUID("55555555-5555-4555-8555-555555555555")
CONTEXT = OrganizationContext(organization_id=ORG_A)

PAGES = {1: (1700, 2200)}
BOX = ((100.0, 200.0), (400.0, 200.0), (400.0, 260.0), (100.0, 260.0))


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/fields.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def test_evidence_serialization_roundtrips() -> None:
    region = Evidence(
        page_number=1, certainty=EvidenceCertainty.REGION, polygon=BOX, quote="PO-100042"
    )
    assert Evidence.from_json(region.to_json()) == region
    page_level = Evidence(page_number=2, certainty=EvidenceCertainty.PAGE)
    assert Evidence.from_json(page_level.to_json()) == page_level
    assert page_level.to_json()["polygon"] is None


def test_evidence_certainty_is_explicit_and_coordinates_never_fabricated() -> None:
    # Region evidence without real coordinates is refused.
    with pytest.raises(ValueError, match="requires a polygon"):
        Evidence(page_number=1, certainty=EvidenceCertainty.REGION)
    with pytest.raises(ValueError, match="requires a polygon"):
        Evidence(page_number=1, certainty=EvidenceCertainty.REGION, polygon=BOX[:2])
    # Page-level evidence carrying coordinates is a contradiction.
    with pytest.raises(ValueError, match="must not carry a polygon"):
        Evidence(page_number=1, certainty=EvidenceCertainty.PAGE, polygon=BOX)


def test_candidate_serialization_and_bounds() -> None:
    candidate = Candidate(raw_value="PO-1000A2", confidence=0.12)
    assert Candidate.from_json(candidate.to_json()) == candidate
    with pytest.raises(ValueError, match="within"):
        Candidate(raw_value="x", confidence=1.2)


async def test_header_and_cell_fields_roundtrip_through_the_database(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            field_key="po_number",
            raw_value="PO-100042",
            confidence=0.99,
            provider="mock",
            provider_model="mock-v1",
            evidence=(
                Evidence(
                    page_number=1,
                    certainty=EvidenceCertainty.REGION,
                    polygon=BOX,
                    quote="PO-100042",
                ),
            ),
            candidates=(Candidate("PO-1000A2", 0.12),),
            page_dimensions=PAGES,
        )
        # A cell: same anchoring, plus row identity.
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            field_key="lines.sku",
            raw_value="WID-100",
            confidence=0.95,
            provider="mock",
            row_index=0,
            evidence=(Evidence(page_number=1, certainty=EvidenceCertainty.PAGE),),
            page_dimensions=PAGES,
        )

    async with db.session_scope() as session:
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(RUN)
        by_key = {(f.field_key, f.row_index): f for f in fields}
        header = by_key[("po_number", None)]
        assert header.raw_value == "PO-100042"
        (span,) = header.evidence_spans()
        assert span.certainty is EvidenceCertainty.REGION
        assert span.polygon == BOX
        assert header.candidate_readings() == (Candidate("PO-1000A2", 0.12),)
        assert header.validation_status == ValidationStatus.PENDING.value
        cell = by_key[("lines.sku", 0)]
        (cell_span,) = cell.evidence_spans()
        assert cell_span.certainty is EvidenceCertainty.PAGE
        assert cell_span.polygon is None


async def test_fabricated_coordinates_are_refused_at_the_door(db: DatabaseSessions) -> None:
    out_of_bounds = ((100.0, 200.0), (2000.0, 200.0), (2000.0, 260.0))
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="leaves page 1 bounds"):
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                field_key="po_number",
                raw_value="PO-100042",
                confidence=0.9,
                provider="mock",
                evidence=(
                    Evidence(
                        page_number=1, certainty=EvidenceCertainty.REGION, polygon=out_of_bounds
                    ),
                ),
                page_dimensions=PAGES,
            )
        with pytest.raises(ValueError, match="run does not have"):
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                field_key="po_number",
                raw_value="PO-100042",
                confidence=0.9,
                provider="mock",
                evidence=(Evidence(page_number=7, certainty=EvidenceCertainty.PAGE),),
                page_dimensions=PAGES,
            )
        with pytest.raises(ValueError, match="absent value cannot carry evidence"):
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                field_key="po_number",
                raw_value=None,
                confidence=0.0,
                provider="mock",
                evidence=(Evidence(page_number=1, certainty=EvidenceCertainty.PAGE),),
            )


async def test_one_row_per_run_field_and_row_including_headers(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        for row in (0, 1):
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                field_key="lines.sku",
                raw_value=f"SKU-{row}",
                confidence=0.9,
                provider="mock",
                row_index=row,
            )
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            field_key="po_number",
            raw_value="PO-1",
            confidence=0.9,
            provider="mock",
        )
    with pytest.raises(IntegrityError):
        async with db.session_scope() as session:
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                field_key="po_number",  # duplicate header field for the run
                raw_value="PO-2",
                confidence=0.9,
                provider="mock",
            )


async def test_promoting_a_candidate_swaps_readings_but_keeps_evidence(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        field = await create_extracted_field(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            field_key="po_number",
            raw_value="PO-1000A2",
            confidence=0.60,
            provider="mock",
            evidence=(Evidence(page_number=1, certainty=EvidenceCertainty.REGION, polygon=BOX),),
            candidates=(Candidate("PO-100042", 0.35),),
            normalized_value="PO-1000A2",
        )
        field.validation_status = ValidationStatus.REVIEW.value
        promote_candidate(field, 0)
        assert field.raw_value == "PO-100042"
        assert field.confidence == 0.35
        # The rejected reading is preserved as a candidate.
        assert field.candidate_readings() == (Candidate("PO-1000A2", 0.60),)
        # Same region, different transcription: evidence is untouched.
        (span,) = field.evidence_spans()
        assert span.polygon == BOX
        # Earlier normalization/validation no longer applies.
        assert field.normalized_value is None
        assert field.validation_status == ValidationStatus.PENDING.value
        with pytest.raises(ValueError, match="no candidate at index"):
            promote_candidate(field, 5)


async def test_summaries_report_honest_counts(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        for key, value, confidence in (
            ("po_number", "PO-1", 0.9),
            ("currency", "USD", 0.7),
            ("delivery_terms", None, 0.0),
        ):
            await create_extracted_field(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                field_key=key,
                raw_value=value,
                confidence=confidence,
                provider="mock",
            )
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(RUN)
    summary = confidence_summary(fields)
    assert summary == {
        "total_fields": 3,
        "extracted": 2,
        "absent": 1,
        "min_confidence": 0.7,
        "mean_confidence": 0.8,
    }
    statuses = validation_summary(fields)
    assert statuses["pending"] == 3
    assert statuses["passed"] == statuses["review"] == statuses["blocked"] == 0
    assert confidence_summary([]) == {
        "total_fields": 0,
        "extracted": 0,
        "absent": 0,
        "min_confidence": None,
        "mean_confidence": None,
    }


async def test_extracted_fields_are_tenant_scoped(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await create_extracted_field(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            field_key="po_number",
            raw_value="PO-1",
            confidence=0.9,
            provider="mock",
        )
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await ExtractedFieldRepository(session, other).list_for_run(RUN) == []

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert "extracted_fields" in RLS_PROTECTED_TABLES
