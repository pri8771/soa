"""Stage executors: the working pipeline (PRC-012).

Wires the real pieces into the PRC-003 orchestrator: render (PRC-004,
sandboxed) -> page rows + page-image artifacts (PRC-005) -> extraction
via the provider contract (PRC-006; the mock or a configured AIO model
adapter, fed native PDF text per page where AIO-002 can read it) ->
extracted-field rows with evidence (PRC-007) -> canonical normalization
(PRC-008) -> rule evaluation (PRC-009/010) -> confidence routing
(PRC-011). Classification and splitting are honest single-document
no-ops until their real implementations land (AIO); their stage runs
say exactly that.

Configuration comes in as an explicit PipelineConfig value. PRC-012
ships the canonical sales-order config; resolving a pinned stream
version (the run's stream_version_id/config_fingerprint) into a
PipelineConfig is the remaining wiring and is NOT faked here — the
worker runs the canonical config until that lands.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.artifacts import ArtifactKind, ArtifactRepository, create_artifact
from soa_db.documents import Document
from soa_db.duplicate_policy import DEFAULT_POLICY, DuplicatePolicy, get_duplicate_policy
from soa_db.extracted_fields import (
    Candidate,
    Evidence,
    EvidenceCertainty,
    ExtractedField,
    ExtractedFieldRepository,
    ValidationStatus,
    confidence_summary,
    create_extracted_field,
)
from soa_db.pages import DocumentPageRepository, create_page
from soa_db.repository import OrganizationContext
from soa_db.review_tasks import route_document_to_review
from soa_db.runs import ProcessingRun, StageRun
from soa_db.stream_config import pinned_stream_config
from soa_db.usage_ledger import record_usage
from soa_normalize import NormalizationContext, NormalizationError, normalize
from soa_rules import (
    ConfidencePolicy,
    EvaluationInput,
    FieldSignal,
    decide_route,
    evaluate_rule_set,
)
from soa_rules.baseline import (
    BASELINE_VERSION,
    CANONICAL_CRITICALITY,
    CANONICAL_NORMALIZER_OVERRIDES,
    TYPE_DEFAULT_NORMALIZERS,
    baseline_sales_order_rules,
)
from soa_storage.keys import artifact_key
from soa_storage.store import ObjectNotFoundError, ObjectStore
from soa_worker.evidence_resolver import (
    TextGeometry,
    geometry_from_native_page,
    resolve_quote,
)
from soa_worker.extraction.mock import PROVIDER_NAME as MOCK_PROVIDER_NAME
from soa_worker.extraction.mock import SYNTHETIC_SALES_ORDER_FIELD_SPECS
from soa_worker.extraction.provider import (
    EvidenceSpan,
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    FieldSpec,
    PageInput,
)
from soa_worker.extraction_repair import RepairableExtractionProvider, extract_with_repair
from soa_worker.orchestrator import StageExecutionError, StageExecutor, StageOutcome
from soa_worker.providers import Capability, create_provider
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextPage,
    NativeTextRequest,
)
from soa_worker.rendering import RenderError, RenderLimits, render_document

ACTOR = "system:pipeline"


class _RepairChannelProvider:
    """Adapt an :class:`ExtractionProvider` to the AIO-012 repair
    interface so the extracting stage can run every provider through
    :func:`extract_with_repair` uniformly. Model adapters expose the
    ``repair_hint`` channel natively and receive it; the mock never emits
    :class:`ModelOutputInvalidError`, so it is only ever called without a
    hint and a plain ``extract`` suffices — the mock path is unchanged."""

    def __init__(self, provider: ExtractionProvider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        return self._provider.name

    async def extract(
        self, request: ExtractionRequest, *, repair_hint: str | None = None
    ) -> ExtractionResult:
        if repair_hint is None:
            return await self._provider.extract(request)
        # A hint only ever follows a ModelOutputInvalidError, which only
        # repairable (model) adapters raise — so this branch is theirs.
        repairable = cast(RepairableExtractionProvider, self._provider)
        return await repairable.extract(request, repair_hint=repair_hint)


def _resolve_evidence(span: EvidenceSpan, geometry_by_page: Mapping[int, TextGeometry]) -> Evidence:
    """AIO-014: turn one provider evidence span into stored evidence with
    HONEST certainty. A span whose verbatim quote resolves to real
    positioned text on its page (native-text geometry) becomes a REGION
    carrying those resolved coordinates; anything else — no quote, no
    geometry for the page (the mock, or a scanned original), or a quote
    that does not match — is page-level, and no polygon is ever
    fabricated."""
    geometry = geometry_by_page.get(span.page_number)
    if span.quote and geometry is not None:
        resolved = resolve_quote(span.quote, geometry)
        if resolved.match_kind in ("exact", "fuzzy"):
            return Evidence(
                page_number=span.page_number,
                certainty=EvidenceCertainty.REGION,
                polygon=resolved.polygon,
                quote=span.quote,
            )
    return Evidence(
        page_number=span.page_number,
        certainty=EvidenceCertainty.PAGE,
        polygon=None,
        quote=span.quote,
    )


@dataclass(frozen=True)
class PipelineConfig:
    """Everything the stages need, as one explicit value: what to
    extract, how to canonicalize it, what to check, and how to route."""

    field_specs: tuple[FieldSpec, ...]
    #: Flat field key -> critical | standard | informational (CFG-003).
    criticality: Mapping[str, str]
    rules: list[dict[str, Any]]
    rules_version: str
    normalization: NormalizationContext = dataclass_field(default_factory=NormalizationContext)
    #: Per-key normalizer overrides (e.g. identifiers); type default otherwise.
    normalizer_overrides: Mapping[str, str] = dataclass_field(default_factory=dict)
    confidence_policy: ConfidencePolicy = dataclass_field(default_factory=ConfidencePolicy)
    render_limits: RenderLimits = dataclass_field(default_factory=RenderLimits)

    def normalizer_for(self, spec: FieldSpec) -> str | None:
        override = self.normalizer_overrides.get(spec.key)
        return override or TYPE_DEFAULT_NORMALIZERS.get(spec.field_type)


def canonical_sales_order_config() -> PipelineConfig:
    """The platform's stock sales-order pipeline configuration —
    the shared canonical schema (soa_rules.baseline) + PRC-010 baseline
    rules + PRC-011 baseline policy, US-locale normalization."""
    return PipelineConfig(
        field_specs=SYNTHETIC_SALES_ORDER_FIELD_SPECS,
        criticality=CANONICAL_CRITICALITY,
        rules=baseline_sales_order_rules(),
        rules_version=BASELINE_VERSION,
        normalization=NormalizationContext(locale="en-US", currency="USD"),
        normalizer_overrides=CANONICAL_NORMALIZER_OVERRIDES,
    )


def build_executors(
    store: ObjectStore,
    provider: ExtractionProvider,
    config: PipelineConfig | None = None,
) -> dict[str, StageExecutor]:
    """The executor registry for Orchestrator(db, executors)."""
    pipeline = _Pipeline(store, provider, config or canonical_sales_order_config())
    return {
        "preprocessing": pipeline.preprocessing,
        "classifying": pipeline.classifying,
        "splitting": pipeline.splitting,
        "extracting": pipeline.extracting,
        "normalizing": pipeline.normalizing,
        "validating_data": pipeline.validating_data,
    }


class _Pipeline:
    def __init__(
        self, store: ObjectStore, provider: ExtractionProvider, config: PipelineConfig
    ) -> None:
        self._store = store
        self._provider = provider
        self._repairable = _RepairChannelProvider(provider)
        self._config = config

    # -- preprocessing: render the original into bounded page rasters -------

    async def preprocessing(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        artifacts = await ArtifactRepository(session, context).list_for_document(document.id)
        originals = [a for a in artifacts if a.kind == ArtifactKind.ORIGINAL.value]
        if not originals:
            raise StageExecutionError(
                "the document has no original artifact to render", retryable=False
            )
        original = originals[0]
        try:
            data = await self._store.get(original.object_key)
        except ObjectNotFoundError:
            raise StageExecutionError(
                "the original object is missing from storage", retryable=True
            ) from None
        try:
            pages = await render_document(
                data,
                content_type=original.content_type,
                limits=self._config.render_limits,
            )
        except RenderError as error:
            raise StageExecutionError(str(error), retryable=error.retryable) from None

        for page in pages:
            key = artifact_key(
                context.organization_id,
                document.id,
                kind="page_image",
                filename=f"page-{page.page_number:04}.png",
            )
            metadata = await self._store.put(key, page.image_png, content_type="image/png")
            artifact = await create_artifact(
                session,
                context,
                document_id=document.id,
                kind=ArtifactKind.PAGE_IMAGE,
                object_key=key,
                sha256=metadata.sha256,
                size_bytes=len(page.image_png),
                content_type="image/png",
                produced_by_run_id=run.id,
                produced_by_stage="preprocessing",
                actor_id=ACTOR,
            )
            await create_page(
                session,
                context,
                document_id=document.id,
                run_id=run.id,
                page_number=page.page_number,
                width_px=page.width_px,
                height_px=page.height_px,
                dpi=page.dpi,
                image_artifact_id=artifact.id,
            )
        return StageOutcome(output_summary={"pages": len(pages)})

    # -- classifying / splitting: honest placeholders until AIO -------------

    async def classifying(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        return StageOutcome(
            output_summary={
                "document_type": "sales_order",
                "method": "assumed",
                "note": "single-type pipeline; a real classifier arrives with AIO",
            }
        )

    async def splitting(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        return StageOutcome(
            output_summary={
                "documents": 1,
                "note": "no split performed; a real splitter arrives with AIO",
            }
        )

    # -- extracting: provider contract -> extracted-field rows ----------------

    async def extracting(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        pages = await DocumentPageRepository(session, context).list_for_run(run.id)
        if not pages:
            raise StageExecutionError("no rendered pages to extract from", retryable=False)
        # ONE native-text read yields both the per-page prompt text and
        # the per-page geometry AIO-014 resolves quotes against.
        native_pages = await self._native_pages(session, context, document)
        text_by_page = {
            number: "\n".join(span.text for span in page.spans)
            for number, page in native_pages.items()
        }
        geometry_by_page = {
            number: geometry_from_native_page(page) for number, page in native_pages.items()
        }
        request = ExtractionRequest(
            document_id=document.id,
            document_sha256=document.content_sha256,
            content_type=document.content_type,
            pages=tuple(
                PageInput(
                    page_number=p.page_number,
                    width_px=p.width_px,
                    height_px=p.height_px,
                    text=text_by_page.get(p.page_number),
                )
                for p in pages
            ),
            fields=self._config.field_specs,
        )
        try:
            # AIO-012: bounded in-call repair. A passthrough for the mock
            # (it never returns malformed output); model adapters re-ask
            # on ModelOutputInvalidError and fall back to an honest
            # all-absent result once the attempt/cost ceilings are spent.
            repaired = await extract_with_repair(self._repairable, request)
        except ExtractionProviderError as error:
            raise StageExecutionError(str(error), retryable=error.retryable) from None
        result = repaired.result

        dimensions = {p.page_number: (p.width_px, p.height_px) for p in pages}
        for extracted in result.fields:
            await create_extracted_field(
                session,
                context,
                document_id=document.id,
                run_id=run.id,
                field_key=extracted.field_key,
                raw_value=extracted.raw_value,
                confidence=extracted.confidence,
                provider=result.provider,
                provider_model=result.model,
                row_index=extracted.row_index,
                evidence=tuple(
                    _resolve_evidence(span, geometry_by_page) for span in extracted.evidence
                ),
                candidates=tuple(
                    Candidate(c.raw_value, c.confidence) for c in extracted.candidates
                ),
                page_dimensions=dimensions,
            )
        # ANA-003 usage ledger: record this run's extraction consumption
        # in the SAME transaction as the stage, so billing/quota/analytics
        # cannot drift from what actually processed. The document-once and
        # reprocess-page rules are applied later at statement time
        # (soa_db.billing_statement); here we record the run's real facts,
        # idempotent on the (run, stage) reference so a retried delivery
        # cannot double-bill.
        await record_usage(
            session,
            context,
            provider=result.provider,
            provider_model=result.model,
            cost_category="extraction",
            estimated_cost_cents=result.cost_cents,
            page_count=len(pages),
            billed_unit="pages",
            billed_quantity=len(pages),
            stream_id=document.stream_id,
            document_id=document.id,
            run_id=run.id,
            source_reference=f"{run.id}:extracting",
            actor_id=ACTOR,
        )
        rows = await ExtractedFieldRepository(session, context).list_for_run(run.id)
        summary = confidence_summary(rows)
        summary["warnings"] = list(result.warnings)
        return StageOutcome(
            output_summary=summary, cost_cents=result.cost_cents, provider=result.provider
        )

    async def _native_pages(
        self, session: AsyncSession, context: OrganizationContext, document: Document
    ) -> dict[int, NativeTextPage]:
        """Per-page native PDF text (AIO-002), read ONCE. Each page it
        returns carries both the document's own words (the extracting
        stage joins them for ``PageInput.text``, so a real provider gets
        the document's words) and the positioned spans AIO-014 resolves
        quotes against. The mock reads nothing and must never spawn the
        native-text sandbox; a document native text cannot read — a
        scanned or image original — honestly yields nothing, so those
        pages get neither prompt text nor geometry."""
        if self._provider.name == MOCK_PROVIDER_NAME:
            return {}
        artifacts = await ArtifactRepository(session, context).list_for_document(document.id)
        originals = [
            a
            for a in artifacts
            if a.kind == ArtifactKind.ORIGINAL.value and a.content_type == "application/pdf"
        ]
        if not originals:
            return {}
        original = originals[0]
        try:
            data = await self._store.get(original.object_key)
        except ObjectNotFoundError:
            return {}
        reader = create_provider(Capability.NATIVE_TEXT, "pdfium-native-text")
        try:
            result = await reader.read(
                NativeTextRequest(
                    document_id=document.id,
                    document_sha256=document.content_sha256,
                    content_type=original.content_type,
                    data=data,
                )
            )
        except NativeTextError:
            return {}
        return {page.page_number: page for page in result.pages if page.spans}

    # -- normalizing: raw -> canonical, never overwriting raw ------------------

    async def normalizing(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        specs = {spec.key: spec for spec in self._config.field_specs}
        rows = await ExtractedFieldRepository(session, context).list_for_run(run.id)
        normalized = failed = skipped = 0
        for row in rows:
            if row.raw_value is None:
                continue
            spec = specs.get(row.field_key)
            kind = self._config.normalizer_for(spec) if spec else None
            if spec is None or kind is None:
                skipped += 1
                continue
            try:
                row.normalized_value = normalize(
                    kind,
                    row.raw_value,
                    context=self._config.normalization,
                    enum_values=spec.enum_values,
                )
                row.normalization_error = None
                normalized += 1
            except NormalizationError as error:
                row.normalized_value = None
                row.normalization_error = str(error)
                failed += 1
        return StageOutcome(
            output_summary={"normalized": normalized, "failed": failed, "skipped": skipped}
        )

    # -- validating: rules + confidence -> route ---------------------------------

    async def validating_data(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
    ) -> StageOutcome:
        rows = await ExtractedFieldRepository(session, context).list_for_run(run.id)
        # Policy gates read the run's PINNED stream config (CFG-002) —
        # a run without a pinned version gets the platform default.
        stream_config = (
            await pinned_stream_config(session, context, run.stream_version_id)
            if run.stream_version_id is not None
            else {}
        )
        data = _evaluation_input(
            rows, document, duplicate_policy=get_duplicate_policy(stream_config)
        )
        evaluation = evaluate_rule_set(self._config.rules, data)
        signals = [
            FieldSignal(
                field_key=row.field_key,
                criticality=self._config.criticality.get(row.field_key, "standard"),
                present=row.raw_value is not None,
                confidence=row.confidence,
                has_evidence=bool(row.evidence_json),
                row_index=row.row_index,
                top_candidate_confidence=max(
                    (c.confidence for c in row.candidate_readings()), default=None
                ),
            )
            for row in rows
        ]
        decision = decide_route(signals, evaluation, self._config.confidence_policy)

        flagged = {
            (reason.field_key, reason.row_index)
            for reason in decision.reasons
            if reason.field_key is not None
        }
        for row in rows:
            if (row.field_key, row.row_index) in flagged:
                row.validation_status = ValidationStatus.REVIEW.value
            elif row.raw_value is not None:
                row.validation_status = ValidationStatus.PASSED.value
        if decision.route == "review_required":
            # REV-001 routing: the review task carries the decision's own
            # reasons, so the reviewer sees exactly why it landed there.
            await route_document_to_review(
                session,
                context,
                document_id=document.id,
                run_id=run.id,
                reasons=[reason.to_json() for reason in decision.reasons],
                priority=document.priority,
                blocking=evaluation.blocking,
                sla_due_at=document.sla_due_at,
                actor_id=ACTOR,
            )
        return StageOutcome(
            output_summary={
                "rules_version": self._config.rules_version,
                "evaluation": evaluation.summary(),
                "decision": decision.to_json(),
            },
            route=decision.route,
        )


def _evaluation_input(
    rows: list[ExtractedField],
    document: Document,
    *,
    duplicate_policy: DuplicatePolicy = DEFAULT_POLICY,
) -> EvaluationInput:
    """PRC-007 rows -> PRC-009 input: canonical value when normalization
    produced one, raw otherwise; table cells grouped into row dicts; the
    ING-006 duplicate flag exposed as meta.duplicate_of."""
    header: dict[str, Any] = {}
    tables: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        value = row.normalized_value if row.normalized_value is not None else row.raw_value
        if row.row_index is None:
            header[row.field_key] = value
        else:
            table = row.field_key.partition(".")[0]
            tables.setdefault(table, {})[row.row_index] = {
                **tables.get(table, {}).get(row.row_index, {}),
                row.field_key: value,
            }
    # An 'allow' policy is processing-transparent: duplicate_of and the
    # audit event still record the duplicate (never silent), but the
    # baseline duplicates.business_hook rule must not see the flag and
    # route to review. When per-stream rules land, reconcile this gate
    # with duplicates.business_hook itself.
    if document.duplicate_of is not None and duplicate_policy is not DuplicatePolicy.ALLOW:
        header["meta.duplicate_of"] = str(document.duplicate_of)
    return EvaluationInput(
        header=header,
        tables={
            table: [cells for _, cells in sorted(by_row.items())]
            for table, by_row in tables.items()
        },
    )
