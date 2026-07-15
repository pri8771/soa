"""Mock end-to-end pipeline (PRC-012): a real file through render ->
extract -> normalize -> validate -> route, with artifacts, timeline,
audit projection, and worker-kill recovery."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, ArtifactRepository, create_artifact
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.extracted_fields import EvidenceCertainty, ExtractedFieldRepository
from soa_db.jobs import Job, JobStatus
from soa_db.pages import DocumentPageRepository
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRunRepository, StageRunRepository
from soa_db.state_projection import verify_state_projection
from soa_db.stream_config import stream_versions_read
from soa_db.usage_ledger import UsageEntry
from soa_storage.keys import artifact_key
from soa_storage.memory import MemoryObjectStore
from soa_storage.store import sha256_hex
from soa_worker.extraction.mock import (
    SYNTHETIC_SALES_ORDER,
    MockExtractionProvider,
    MockMode,
)
from soa_worker.extraction.provider import ExtractionRequest, ExtractionResult
from soa_worker.orchestrator import STAGE_SEQUENCE, Orchestrator
from soa_worker.pipeline import build_executors
from soa_worker.providers.native_text import NativeTextPage, NativeTextResult, TextSpan

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONTEXT = OrganizationContext(organization_id=ORG)

#: A REAL digital PDF (the AIO-002 corpus) whose native text the
#: extracting stage must hand to non-mock providers.
DIGITAL_PO = (Path(__file__).parent / "fixtures" / "pdfs" / "digital-po.pdf").read_bytes()


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/pipeline.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # The worker never imports the API's stream-version model; create
        # the read-side projection's table for pinned-config tests. The
        # drop keeps the shape minimal even when another test module in
        # the same process imported the full model into Base.metadata.
        await conn.exec_driver_sql("DROP TABLE IF EXISTS stream_versions")
        await conn.run_sync(stream_versions_read.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_document(
    db: DatabaseSessions, store: MemoryObjectStore, data: bytes = SYNTHETIC_SALES_ORDER
) -> uuid.UUID:
    """A queued document with its original stored — where intake leaves it."""
    sha = sha256_hex(data)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha,
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        key = artifact_key(ORG, document.id, kind="original", filename="po.pdf")
        await store.put(key, data, content_type="application/pdf")
        await create_artifact(
            session,
            CONTEXT,
            document_id=document.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=sha,
            size_bytes=len(data),
            content_type="application/pdf",
        )
        for state in (DocumentState.VALIDATING_FILE, DocumentState.QUEUED):
            await transition_document(
                session, CONTEXT, document=document, to_state=state, actor_id="worker"
            )
        return document.id


def preprocess_payload(document_id: uuid.UUID) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "organization_id": str(ORG),
        "stream_id": str(STREAM),
        "stream_version_id": None,
        "config_fingerprint": "f" * 64,
    }


async def pump(db: DatabaseSessions, orchestrator: Orchestrator, *, deliveries: int = 50) -> int:
    delivered = 0
    processed: set[uuid.UUID] = set()
    for _ in range(deliveries):
        async with db.session_scope() as session:
            jobs = (
                (
                    await session.execute(
                        select(Job).where(
                            Job.job_type == "document.stage",
                            Job.status == JobStatus.PENDING.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            pending = [j for j in jobs if j.id not in processed]
            if not pending:
                return delivered
            job = pending[0]
            processed.add(job.id)
            payload = dict(job.payload)
        await orchestrator.handle_stage(payload)
        delivered += 1
    return delivered


async def test_synthetic_sales_order_reaches_approved_with_full_record(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"

        # The audit trail replays to exactly the stored state — the
        # timeline is complete and consistent.
        await verify_state_projection(session, CONTEXT, document)

        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert run.state == "succeeded"
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        assert sorted({s.stage for s in stages}) == sorted(STAGE_SEQUENCE)
        by_stage = {s.stage: s for s in stages}
        assert all(s.state == "succeeded" for s in stages)
        assert all(s.latency_ms is not None for s in stages), "metrics recorded per stage"

        # Rendered pages exist as rows AND as stored artifacts.
        pages = await DocumentPageRepository(session, CONTEXT).list_for_run(run.id)
        assert [p.page_number for p in pages] == [1]
        assert by_stage["preprocessing"].output_summary == {"pages": 1}
        artifacts = await ArtifactRepository(session, CONTEXT).list_for_document(document_id)
        page_images = [a for a in artifacts if a.kind == ArtifactKind.PAGE_IMAGE.value]
        assert len(page_images) == 1
        assert page_images[0].produced_by_run_id == run.id
        stored = await store.get(page_images[0].object_key)
        assert stored.startswith(b"\x89PNG")

        # Extracted fields with evidence, canonical values, and statuses.
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(run.id)
        by_key = {(f.field_key, f.row_index): f for f in fields}
        po = by_key[("po_number", None)]
        assert po.raw_value == "PO-100042"
        assert po.normalized_value == "PO-100042"
        # AIO-014 honesty: the mock has no native-text geometry, so its
        # evidence is page-level — no fabricated full-page polygon, but
        # the verbatim quote is preserved.
        span = po.evidence_spans()[0]
        assert span.certainty == EvidenceCertainty.PAGE
        assert span.polygon is None
        assert span.quote == "PO-100042"
        assert po.validation_status == "passed"
        assert by_key[("order_date", None)].normalized_value == "2026-03-14"
        assert by_key[("requested_delivery_date", None)].normalized_value == "2026-04-01"
        assert by_key[("total_amount", None)].normalized_value == {
            "amount": "1234.50",
            "currency": "USD",
        }
        assert by_key[("lines.quantity", 1)].normalized_value == "3"
        # The deliberately-missing field stayed honestly absent.
        assert by_key[("delivery_terms", None)].raw_value is None

        # The routing decision is on the record with its reasons.
        validating = by_stage["validating_data"]
        assert validating.output_summary["decision"]["route"] == "approved"
        assert validating.output_summary["decision"]["reasons"] == []
        assert validating.output_summary["evaluation"]["blocking"] is False
        assert validating.output_summary["rules_version"] == "1.1.0"
        assert by_stage["extracting"].provider == "mock"

        # ANA-003 usage ledger: the extracting stage recorded this run's
        # consumption as an immutable ledger row, so billing/quota/cost
        # analytics see real facts instead of zero.
        usage = (
            (await session.execute(select(UsageEntry).where(UsageEntry.organization_id == ORG)))
            .scalars()
            .all()
        )
        extraction_usage = [e for e in usage if e.cost_category == "extraction"]
        assert len(extraction_usage) == 1
        entry = extraction_usage[0]
        assert entry.entry_type == "usage"
        assert entry.provider == "mock"
        assert entry.document_id == document_id
        assert entry.run_id == run.id
        assert entry.stream_id == STREAM
        assert entry.page_count == 1
        assert entry.billed_unit == "pages"
        assert entry.billed_quantity == 1
        assert entry.source_reference == f"{run.id}:extracting"


async def test_worker_kill_mid_pipeline_resumes_from_the_database(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    first = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await first.handle_preprocess(preprocess_payload(document_id))
    # The worker dies after two stages...
    await pump(db, first, deliveries=2)
    # ...and a fresh process picks up exactly where the database says.
    second = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await pump(db, second)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"
        await verify_state_projection(session, CONTEXT, document)
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        # Every stage ran exactly once — no duplicated work, no gaps.
        assert sorted(s.stage for s in stages) == sorted(STAGE_SEQUENCE)
        assert all(s.attempt == 1 and s.state == "succeeded" for s in stages)


async def test_low_confidence_extraction_routes_to_review(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    provider = MockExtractionProvider(mode=MockMode.LOW_CONFIDENCE)
    orchestrator = Orchestrator(db, build_executors(store, provider))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "review_required"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        decision = {s.stage: s for s in stages}["validating_data"].output_summary["decision"]
        assert decision["route"] == "review_required"
        assert any(r["code"] == "low_confidence" for r in decision["reasons"])
        # Flagged fields carry review status for the REV epic to pick up.
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(run.id)
        assert any(f.validation_status == "review" for f in fields)
        # REV-001 routing: the review task exists, linked to this run,
        # carrying the decision's own reasons.
        from soa_db.review_tasks import ReviewTaskRepository

        task = await ReviewTaskRepository(session, CONTEXT).get_active_for_document(document_id)
        assert task is not None
        assert task.run_id == run.id
        assert task.state == "open"
        assert any(r["code"] == "low_confidence" for r in task.reasons)
        assert all(r.get("field_key") or r.get("rule_key") for r in task.reasons)


async def test_terminal_extraction_failure_parks_the_document(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    provider = MockExtractionProvider(mode=MockMode.ERROR_TERMINAL)
    orchestrator = Orchestrator(db, build_executors(store, provider))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "failed_terminal"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert run.state == "failed"
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        extracting = [s for s in stages if s.stage == "extracting"]
        assert [(s.attempt, s.state, s.failure_class) for s in extracting] == [
            (1, "failed", "terminal")
        ]


async def test_unrecognized_document_reviews_instead_of_inventing_values(
    db: DatabaseSessions,
) -> None:
    # A structurally valid PDF the mock does NOT know: every field comes
    # back absent, required-field rules fire, and a human gets it.
    unknown_pdf = SYNTHETIC_SALES_ORDER.replace(b"SO-FIXTURE-001", b"SO-UNKNOWN-999")
    store = MemoryObjectStore()
    document_id = await seed_document(db, store, data=unknown_pdf)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "review_required"
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(
            (await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id))[0].id
        )
        assert all(f.raw_value is None for f in fields), "nothing was invented"


# -- native PDF text for real providers (AIO-002 feeding PRC-006) -----------


class _CapturingProvider:
    """A non-mock-named provider that records the requests the pipeline
    builds (so the test can see exactly what a real model would) and
    delegates the answers to the mock."""

    def __init__(self) -> None:
        self.requests: list[ExtractionRequest] = []
        self._inner = MockExtractionProvider()

    @property
    def name(self) -> str:
        return "capturing-test-provider"

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.requests.append(request)
        return await self._inner.extract(request)


async def test_non_mock_providers_receive_native_pdf_text(db: DatabaseSessions) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store, data=DIGITAL_PO)
    provider = _CapturingProvider()
    orchestrator = Orchestrator(db, build_executors(store, provider))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    (request,) = provider.requests
    assert request.pages, "the extraction request carries the rendered pages"
    assert all(page.text for page in request.pages), "every page carries its native text"
    assert "PURCHASE ORDER PO-4711" in (request.pages[0].text or "")


async def test_the_mock_provider_never_spawns_native_text(
    db: DatabaseSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mock reads nothing from page text — the pipeline must not pay
    for (or depend on) the native-text sandbox when it runs."""

    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("the mock path must never resolve the native-text provider")

    monkeypatch.setattr("soa_worker.pipeline.create_provider", _refuse)
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"


# -- honest evidence: quote -> coordinates (AIO-014 wired) -------------------


class _StubNativeText:
    """A native-text provider returning fixed page geometry, so the
    AIO-014 resolver has REAL coordinates to anchor a quote against
    without depending on the pdfium sandbox."""

    def __init__(self, pages: tuple[NativeTextPage, ...]) -> None:
        self._pages = pages

    @property
    def name(self) -> str:
        return "stub-native-text"

    async def read(self, request: object) -> NativeTextResult:
        return NativeTextResult(provider=self.name, pages=self._pages)


#: A real sub-page box (not the full page) where the PO number "sits".
PO_BOX = ((100.0, 100.0), (400.0, 100.0), (400.0, 140.0), (100.0, 140.0))


async def test_matching_quote_resolves_to_a_subpage_region(
    db: DatabaseSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-mock provider's evidence quote that matches injected
    native-text geometry is stored as a REGION at the resolved sub-page
    box — real coordinates, never the fabricated full page. A quote with
    no match on the page stays honestly page-level."""
    page = NativeTextPage(
        page_number=1,
        width_px=1700,
        height_px=2200,
        spans=(TextSpan(text="PO-100042", polygon=PO_BOX),),
        coverage=1.0,
    )
    monkeypatch.setattr(
        "soa_worker.pipeline.create_provider",
        lambda *args, **kwargs: _StubNativeText((page,)),
    )
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    orchestrator = Orchestrator(db, build_executors(store, _CapturingProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(run.id)
        by_key = {(f.field_key, f.row_index): f for f in fields}

        # po_number: the quote resolved to REAL geometry.
        po = by_key[("po_number", None)].evidence_spans()[0]
        assert po.certainty == EvidenceCertainty.REGION
        assert po.polygon == PO_BOX
        full_page = ((0.0, 0.0), (1700.0, 0.0), (1700.0, 2200.0), (0.0, 2200.0))
        assert po.polygon != full_page, "resolved to a sub-page box, not the full page"

        # customer_name: its quote is not on the page — honest PAGE, no
        # fabricated polygon.
        customer = by_key[("customer_name", None)].evidence_spans()[0]
        assert customer.certainty == EvidenceCertainty.PAGE
        assert customer.polygon is None
        assert customer.quote == "Acme Industrial Supply"


async def test_no_native_geometry_keeps_evidence_page_level(
    db: DatabaseSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When native text reads nothing (a scanned original), a non-mock
    provider's evidence has no geometry to resolve against — every span
    is page-level, and no full-page polygon is invented."""
    monkeypatch.setattr(
        "soa_worker.pipeline.create_provider",
        lambda *args, **kwargs: _StubNativeText(()),
    )
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    orchestrator = Orchestrator(db, build_executors(store, _CapturingProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(run.id)
        with_evidence = [f for f in fields if f.evidence_json]
        assert with_evidence, "the provider returned evidence"
        for field in with_evidence:
            for span in field.evidence_spans():
                assert span.certainty == EvidenceCertainty.PAGE
                assert span.polygon is None


# -- duplicate policy at validation (ING-006) --------------------------------


async def seed_stream_version(db: DatabaseSessions, *, duplicate_policy: str) -> uuid.UUID:
    """A published stream version row carrying the pinned policy, as the
    API's publish flow would have frozen it."""
    version_id = uuid.uuid4()
    async with db.session_scope() as session:
        await session.execute(
            stream_versions_read.insert().values(
                id=version_id,
                organization_id=ORG,
                resolved_snapshot={"config": {"duplicate_policy": duplicate_policy}},
            )
        )
    return version_id


async def mark_duplicate(db: DatabaseSessions, document_id: uuid.UUID) -> None:
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        document.duplicate_of = uuid.uuid4()


async def run_duplicate_pipeline(
    db: DatabaseSessions, *, duplicate_policy: str
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Drive the known-good fixture, marked as a duplicate, through a run
    pinned to the given policy; returns (document_id, routing decision)."""
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    await mark_duplicate(db, document_id)
    version_id = await seed_stream_version(db, duplicate_policy=duplicate_policy)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    payload = preprocess_payload(document_id)
    payload["stream_version_id"] = str(version_id)
    await orchestrator.handle_preprocess(payload)
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        decision = {s.stage: s for s in stages}["validating_data"].output_summary["decision"]
    return document_id, dict(decision)


async def test_allow_policy_processes_a_duplicate_transparently(db: DatabaseSessions) -> None:
    document_id, decision = await run_duplicate_pipeline(db, duplicate_policy="allow")
    assert decision["route"] == "approved"
    assert all(r.get("rule_key") != "duplicates.business_hook" for r in decision["reasons"])
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "approved"
        # Transparent, not silent: the duplicate marker survives (ING-006).
        assert document.duplicate_of is not None


async def test_flag_policy_still_routes_a_duplicate_to_review(db: DatabaseSessions) -> None:
    document_id, decision = await run_duplicate_pipeline(db, duplicate_policy="flag")
    assert decision["route"] == "review_required"
    assert any(r.get("rule_key") == "duplicates.business_hook" for r in decision["reasons"])
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "review_required"
