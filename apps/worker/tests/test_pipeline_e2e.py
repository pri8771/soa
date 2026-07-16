"""Mock end-to-end pipeline (PRC-012): a real file through render ->
extract -> normalize -> validate -> route, with artifacts, timeline,
audit projection, and worker-kill recovery."""

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, ArtifactRepository, create_artifact
from soa_db.catalog_business import BusinessValidationResult
from soa_db.catalog_selections import CatalogFieldSelectionRepository
from soa_db.catalogs import (
    CatalogBindingMode,
    activate_catalog_version,
    add_catalog_record,
    bind_catalog_to_stream,
    create_catalog,
    create_catalog_version,
)
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
from soa_db.usage_ledger import UsageEntry
from soa_storage.keys import artifact_key
from soa_storage.memory import MemoryObjectStore
from soa_storage.store import sha256_hex
from soa_worker.extraction.mock import (
    SYNTHETIC_SALES_ORDER,
    MockExtractionProvider,
    MockMode,
)
from soa_worker.extraction.provider import (
    ExtractedField,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
)
from soa_worker.model_usage import ProviderCallUsage, ProviderUsage
from soa_worker.orchestrator import STAGE_SEQUENCE, Orchestrator
from soa_worker.pipeline import build_executors, canonical_sales_order_config
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


async def seed_reference_catalogs(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        customers = await create_catalog(
            session,
            CONTEXT,
            name="Customers",
            slug="customers",
            catalog_type="customers",
            source="manual",
            actor_id="user:test",
        )
        customer_version = await create_catalog_version(
            session, CONTEXT, catalog=customers, actor_id="user:test"
        )
        await add_catalog_record(
            session,
            CONTEXT,
            version=customer_version,
            source_id="C-100",
            display_name="Acme Industrial Supply",
            attributes={"roles": ["sold_to"]},
        )
        await activate_catalog_version(
            session,
            CONTEXT,
            catalog=customers,
            version=customer_version,
            actor_id="user:test",
        )
        await bind_catalog_to_stream(
            session,
            CONTEXT,
            stream_id=STREAM,
            catalog=customers,
            mode=CatalogBindingMode.PINNED,
            pinned_version_id=customer_version.id,
            actor_id="user:test",
        )

        products = await create_catalog(
            session,
            CONTEXT,
            name="Products",
            slug="products",
            catalog_type="products",
            source="manual",
            actor_id="user:test",
        )
        product_version = await create_catalog_version(
            session, CONTEXT, catalog=products, actor_id="user:test"
        )
        for source_id, name, price in (
            ("WID-100", "Widget", "45.00"),
            ("GAD-205", "Gadget", "261.50"),
        ):
            await add_catalog_record(
                session,
                CONTEXT,
                version=product_version,
                source_id=source_id,
                display_name=name,
                attributes={"base_uom": "EA", "price": price, "currency": "USD"},
            )
        await activate_catalog_version(
            session,
            CONTEXT,
            catalog=products,
            version=product_version,
            actor_id="user:test",
        )
        await bind_catalog_to_stream(
            session,
            CONTEXT,
            stream_id=STREAM,
            catalog=products,
            mode=CatalogBindingMode.PINNED,
            pinned_version_id=product_version.id,
            actor_id="user:test",
        )


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
        assert by_stage["classifying"].output_summary == {
            "document_type": "purchase_order",
            "method": "input_contract",
            "input_contract": "single_sales_order",
        }

        # Rendered pages exist as rows AND as stored artifacts.
        pages = await DocumentPageRepository(session, CONTEXT).list_for_run(run.id)
        assert [p.page_number for p in pages] == [1]
        preprocessing_summary = by_stage["preprocessing"].output_summary
        assert preprocessing_summary["pages"] == 1
        assert preprocessing_summary["runtime_provenance"]["engine"]["name"] == "pypdfium2"
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
        assert run.runtime_fingerprint is not None
        assert len(run.runtime_fingerprint) == 64
        assert po.execution_fingerprint == run.runtime_fingerprint
        assert run.runtime_provenance is not None
        assert run.runtime_provenance["extraction"]["model"] == "mock-v1"
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
        usage = (await session.execute(select(UsageEntry))).scalars().all()
        assert [
            (
                entry.provider,
                entry.cost_category,
                entry.billed_unit,
                entry.billed_quantity,
                entry.page_count,
            )
            for entry in usage
        ] == [("mock", "extraction", "calls", 1, 1)]


async def test_real_model_provider_receives_recognized_document_text(
    db: DatabaseSessions,
) -> None:
    class CapturingProvider:
        name = "capturing-model"

        def __init__(self) -> None:
            self.request: ExtractionRequest | None = None

        async def extract(self, request: ExtractionRequest) -> ExtractionResult:
            self.request = request
            return ExtractionResult(
                provider=self.name,
                fields=tuple(
                    ExtractedField(field_key=spec.key, raw_value=None, confidence=0.0)
                    for spec in request.fields
                    if spec.field_type != "table"
                ),
                model="metered-model-v1",
                cost_cents=7,
                usage=ProviderUsage(800, 200, 1_050),
                pricing_reference="test-rate-card:v1",
                usage_records=(
                    ProviderCallUsage(
                        provider="failed-model",
                        model="failed-v1",
                        usage=ProviderUsage(400, 100, 500),
                        estimated_cost_cents=2,
                        pricing_reference="failed-rate-card:v1",
                        outcome="invalid_output",
                    ),
                    ProviderCallUsage(
                        provider=self.name,
                        model="metered-model-v1",
                        usage=ProviderUsage(800, 200, 1_050),
                        estimated_cost_cents=5,
                        pricing_reference="test-rate-card:v1",
                        outcome="succeeded",
                    ),
                ),
            )

    data = (Path(__file__).parent / "fixtures" / "pdfs" / "digital-po.pdf").read_bytes()
    store = MemoryObjectStore()
    document_id = await seed_document(db, store, data=data)
    provider = CapturingProvider()
    orchestrator = Orchestrator(db, build_executors(store, provider))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    assert provider.request is not None
    assert "PURCHASE ORDER PO-4711" in (provider.request.pages[0].text or "")
    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        pages = await DocumentPageRepository(session, CONTEXT).list_for_run(run.id)
        assert all(page.text_artifact_id is not None for page in pages)
        artifacts = await ArtifactRepository(session, CONTEXT).list_for_document(document_id)
        assert len([item for item in artifacts if item.kind == ArtifactKind.OCR_TEXT.value]) == 2
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        extracting = next(stage for stage in stages if stage.stage == "extracting")
        assert extracting.output_summary["provider_usage"] == {
            "input_tokens": 800,
            "output_tokens": 200,
            "total_tokens": 1_050,
            "estimated_cost_cents": 5,
            "pricing_reference": "test-rate-card:v1",
        }
        assert extracting.output_summary["estimated_stage_cost_cents"] == 7
        usage_entries = (
            (
                await session.execute(
                    select(UsageEntry).where(UsageEntry.cost_category == "extraction")
                )
            )
            .scalars()
            .all()
        )
        assert len(usage_entries) == 2
        by_provider = {entry.provider: entry for entry in usage_entries}
        assert (
            by_provider["failed-model"].billed_unit,
            by_provider["failed-model"].billed_quantity,
            by_provider["failed-model"].estimated_cost_cents,
        ) == ("tokens", 500, 2)
        assert (
            by_provider["capturing-model"].billed_unit,
            by_provider["capturing-model"].billed_quantity,
            by_provider["capturing-model"].estimated_cost_cents,
        ) == ("tokens", 1_050, 5)
        assert "pricing_reference=failed-rate-card:v1" in (by_provider["failed-model"].reason or "")


async def test_failed_model_call_is_still_ledgered_and_charged_to_the_run(
    db: DatabaseSessions,
) -> None:
    class FailingProvider:
        name = "failing-metered-model"

        async def extract(self, _request: ExtractionRequest) -> ExtractionResult:
            record = ProviderCallUsage(
                provider=self.name,
                model="failed-v1",
                usage=ProviderUsage(300, 50, 350),
                estimated_cost_cents=4,
                pricing_reference="failed-rate:v1",
                outcome="failed",
            )
            raise ExtractionProviderError(
                "the model rejected the request",
                retryable=False,
                usage_records=(record,),
                usage_records_complete=True,
            )

    data = (Path(__file__).parent / "fixtures" / "pdfs" / "digital-po.pdf").read_bytes()
    store = MemoryObjectStore()
    document_id = await seed_document(db, store, data=data)
    orchestrator = Orchestrator(db, build_executors(store, FailingProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        assert run.total_cost_cents == 4
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        extracting = next(stage for stage in stages if stage.stage == "extracting")
        assert extracting.state == "failed"
        assert extracting.cost_cents == 4
        entries = (
            (
                await session.execute(
                    select(UsageEntry).where(UsageEntry.cost_category == "extraction")
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1
        assert (entries[0].provider, entries[0].billed_quantity) == (
            "failing-metered-model",
            350,
        )
        assert entries[0].estimated_cost_cents == 4


async def test_bound_catalogs_match_and_validate_inside_pipeline(db: DatabaseSessions) -> None:
    await seed_reference_catalogs(db)
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider()))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None and document.state == "approved"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        fields = await ExtractedFieldRepository(session, CONTEXT).list_for_run(run.id)
        matched = {
            (field.field_key, field.row_index): field.catalog_match_json
            for field in fields
            if field.catalog_match_json is not None
        }
        assert matched[("customer_name", None)]["selected_source_id"] == "C-100"
        assert matched[("lines.sku", 0)]["selected_source_id"] == "WID-100"
        assert matched[("lines.sku", 1)]["selected_source_id"] == "GAD-205"
        selections = await CatalogFieldSelectionRepository(session, CONTEXT).list_for_run(run.id)
        by_position = {(row.field_key, row.row_index): row for row in selections}
        assert set(by_position) == {
            ("customer_name", None),
            ("lines.sku", 0),
            ("lines.sku", 1),
        }
        assert all(row.catalog_record_id is not None for row in selections)
        assert all(row.catalog_version_id is not None for row in selections)
        assert by_position[("customer_name", None)].source_id == "C-100"
        assert by_position[("lines.sku", 0)].source_id == "WID-100"
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        summary = {stage.stage: stage for stage in stages}["validating_data"].output_summary
        assert summary["business_validation"]["matches"] == 3
        assert summary["business_validation"]["findings"] == []


async def test_business_findings_keep_evaluation_and_decision_summary_aligned(
    db: DatabaseSessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def finding_only(*_args: object, **_kwargs: object) -> BusinessValidationResult:
        return BusinessValidationResult(
            findings=(
                {
                    "code": "customer_unresolved",
                    "message": "customer needs review",
                    "field_key": "customer_name",
                    "row_index": None,
                    "rule_key": "catalog.customer_unresolved",
                },
            ),
            notes=(),
            catalog_versions={},
            matches=0,
        )

    monkeypatch.setattr("soa_worker.pipeline.validate_order_business_data", finding_only)
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    config = replace(
        canonical_sales_order_config(),
        rules=[],
        rules_version="business-finding-only",
    )
    orchestrator = Orchestrator(
        db,
        build_executors(store, MockExtractionProvider(), config),
    )
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        summary = {stage.stage: stage for stage in stages}["validating_data"].output_summary
        assert summary["decision"]["route"] == "review_required"
        assert summary["evaluation"]["review_required"] is True
        assert summary["business_validation"]["findings"][0]["code"] == ("customer_unresolved")

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
        assert entry.billed_unit == "calls"
        assert entry.billed_quantity == 1
        extracting = next(stage for stage in stages if stage.stage == "extracting")
        assert entry.source_reference == f"stage:{extracting.id}:extraction:call:1"


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


async def test_unresolved_native_geometry_keeps_evidence_page_level(
    db: DatabaseSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the provider's quote does not occur in recognized geometry,
    every span remains page-level and no full-page polygon is invented.

    A truly empty native result correctly enters the production OCR fallback;
    this test isolates the evidence resolver without weakening that behavior.
    """
    unrelated_page = NativeTextPage(
        page_number=1,
        width_px=1700,
        height_px=2200,
        spans=(TextSpan(text="unrelated recognized text", polygon=PO_BOX),),
        coverage=1.0,
    )
    monkeypatch.setattr(
        "soa_worker.pipeline.create_provider",
        lambda *args, **kwargs: _StubNativeText((unrelated_page,)),
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
    config = replace(
        canonical_sales_order_config(),
        stream_config={"duplicate_policy": duplicate_policy},
    )
    orchestrator = Orchestrator(
        db,
        build_executors(store, MockExtractionProvider(), config),
    )
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
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


async def test_allow_exact_duplicate_is_not_rerouted_by_business_po_check(
    db: DatabaseSessions,
) -> None:
    """The independent PO check must not undo exact-duplicate ``allow``."""

    store = MemoryObjectStore()
    config = replace(
        canonical_sales_order_config(),
        stream_config={"duplicate_policy": "allow", "business_duplicate_policy": "warn"},
    )
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider(), config))

    original_id = await seed_document(db, store)
    await orchestrator.handle_preprocess(preprocess_payload(original_id))
    await pump(db, orchestrator)

    duplicate_id = await seed_document(db, store)
    async with db.session_scope() as session:
        duplicate = await DocumentRepository(session, CONTEXT).get(duplicate_id)
        assert duplicate is not None
        duplicate.duplicate_of = original_id
    await orchestrator.handle_preprocess(preprocess_payload(duplicate_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        duplicate = await DocumentRepository(session, CONTEXT).get(duplicate_id)
        assert duplicate is not None
        assert duplicate.state == "approved"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(duplicate_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        validation = {stage.stage: stage for stage in stages}["validating_data"]
        summary = validation.output_summary or {}
        assert summary["decision"]["route"] == "approved"
        assert summary["business_validation"]["findings"] == []
        assert any(
            "exact duplicate was excluded" in note
            for note in summary["business_validation"]["notes"]
        )


async def test_flag_policy_still_routes_a_duplicate_to_review(db: DatabaseSessions) -> None:
    document_id, decision = await run_duplicate_pipeline(db, duplicate_policy="flag")
    assert decision["route"] == "review_required"
    assert any(r.get("rule_key") == "duplicates.business_hook" for r in decision["reasons"])
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == "review_required"


async def test_flag_policy_is_enforced_when_custom_rules_omit_duplicate_hook(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    document_id = await seed_document(db, store)
    await mark_duplicate(db, document_id)
    config = replace(
        canonical_sales_order_config(),
        rules=[],
        rules_version="custom-without-duplicate-hook",
        stream_config={"duplicate_policy": "flag"},
    )
    orchestrator = Orchestrator(db, build_executors(store, MockExtractionProvider(), config))
    await orchestrator.handle_preprocess(preprocess_payload(document_id))
    await pump(db, orchestrator)

    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None and document.state == "review_required"
        (run,) = await ProcessingRunRepository(session, CONTEXT).list_for_document(document_id)
        stages = await StageRunRepository(session, CONTEXT).list_for_run(run.id)
        decision = {stage.stage: stage for stage in stages}["validating_data"].output_summary[
            "decision"
        ]
        assert any(
            reason.get("code") == "exact_duplicate_flagged" for reason in decision["reasons"]
        )
