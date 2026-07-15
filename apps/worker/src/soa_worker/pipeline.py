"""Stage executors: the working pipeline (PRC-012).

Wires the real pieces into the PRC-003 orchestrator: render (PRC-004,
sandboxed) -> page rows + page-image artifacts (PRC-005) -> extraction
via the provider contract (PRC-006, mock today, AIO adapters later) ->
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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.artifacts import ArtifactKind, ArtifactRepository, create_artifact
from soa_db.documents import Document
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
from soa_worker.extraction.mock import SYNTHETIC_SALES_ORDER_FIELD_SPECS
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    FieldSpec,
    PageInput,
)
from soa_worker.orchestrator import StageExecutionError, StageExecutor, StageOutcome
from soa_worker.provider_router import (
    NATIVE_COVERAGE_THRESHOLD,
    DocumentFacts,
    RoutingPolicy,
    route,
)
from soa_worker.providers import Capability, create_provider
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextFailure,
    NativeTextProvider,
    NativeTextRequest,
)
from soa_worker.providers.ocr import OcrPageInput, OcrProvider, OcrProviderError, OcrRequest
from soa_worker.rendering import RenderError, RenderLimits, render_document

ACTOR = "system:pipeline"


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
    languages: tuple[str, ...] = ("en",)

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
        text_by_page: dict[int, str] = {}
        text_warnings: list[str] = []
        # Model providers read page text; passing empty PageInput values
        # would produce a syntactically valid request containing no
        # document. The deterministic fixture mock intentionally does not
        # need text, but every real provider does.
        if self._provider.name != "mock":
            text_by_page, text_warnings = await self._recognize_page_text(
                session, context, run, document, pages
            )
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
            result = await self._provider.extract(request)
        except ExtractionProviderError as error:
            raise StageExecutionError(str(error), retryable=error.retryable) from None

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
                instruction_reference=result.instruction_reference,
                config_fingerprint=run.config_fingerprint,
                execution_fingerprint=run.execution_fingerprint,
                row_index=extracted.row_index,
                evidence=tuple(
                    Evidence(
                        page_number=span.page_number,
                        certainty=EvidenceCertainty.REGION,
                        polygon=span.polygon,
                        quote=span.quote,
                    )
                    for span in extracted.evidence
                ),
                candidates=tuple(
                    Candidate(c.raw_value, c.confidence) for c in extracted.candidates
                ),
                page_dimensions=dimensions,
            )
        rows = await ExtractedFieldRepository(session, context).list_for_run(run.id)
        summary = confidence_summary(rows)
        summary["warnings"] = [*text_warnings, *result.warnings]
        summary["provenance"] = {
            "provider": result.provider,
            "model": result.model,
            "instruction_reference": result.instruction_reference,
            "config_fingerprint": run.config_fingerprint,
            "execution_fingerprint": run.execution_fingerprint,
        }
        return StageOutcome(
            output_summary=summary, cost_cents=result.cost_cents, provider=result.provider
        )

    async def _recognize_page_text(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        pages: list[Any],
    ) -> tuple[dict[int, str], list[str]]:
        """Native text first, OCR only for image/low-coverage pages.

        The recognized text is persisted per page and attached to the page
        row, so a model call is attributable and reprocessing does not hide
        what input the model actually saw.
        """
        artifacts = await ArtifactRepository(session, context).list_for_document(document.id)
        original = next((item for item in artifacts if item.kind == ArtifactKind.ORIGINAL), None)
        if original is None:
            raise StageExecutionError("the document has no original artifact", retryable=False)
        try:
            original_bytes = await self._store.get(original.object_key)
        except ObjectNotFoundError:
            raise StageExecutionError("the original object is missing", retryable=True) from None

        texts: dict[int, str] = {}
        warnings: list[str] = []
        needs_ocr = {page.page_number for page in pages}
        if document.content_type == "application/pdf":
            native = create_provider(Capability.NATIVE_TEXT, "pdfium-native-text")
            if not isinstance(native, NativeTextProvider):
                raise StageExecutionError("native text provider is misconfigured", retryable=False)
            try:
                native_result = await native.read(
                    NativeTextRequest(
                        document_id=document.id,
                        document_sha256=document.content_sha256,
                        content_type=document.content_type,
                        data=original_bytes,
                        max_pages=len(pages),
                    )
                )
            except NativeTextError as error:
                if error.failure in (NativeTextFailure.ENCRYPTED, NativeTextFailure.CORRUPT):
                    raise StageExecutionError(str(error), retryable=False) from None
                if error.failure is NativeTextFailure.UNAVAILABLE:
                    raise StageExecutionError(str(error), retryable=error.retryable) from None
                warnings.append(f"native text unavailable ({error.failure.value}); using OCR")
            else:
                warnings.extend(native_result.warnings)
                for result_page in native_result.pages:
                    if result_page.coverage >= NATIVE_COVERAGE_THRESHOLD:
                        texts[result_page.page_number] = "\n".join(
                            span.text for span in result_page.spans
                        )
                        needs_ocr.discard(result_page.page_number)
                    else:
                        warnings.append(
                            f"page {result_page.page_number} native text coverage is "
                            f"{result_page.coverage:.0%}; using OCR"
                        )

        if needs_ocr:
            facts = DocumentFacts(language=self._config.languages[0])
            try:
                decision = route(
                    Capability.OCR,
                    policy=RoutingPolicy(local_only=True),
                    facts=facts,
                )
                ocr = create_provider(Capability.OCR, decision.provider.name)
            except Exception as error:
                raise StageExecutionError(
                    f"OCR is required but no local provider is available ({type(error).__name__})",
                    retryable=False,
                ) from None
            if not isinstance(ocr, OcrProvider):
                raise StageExecutionError("OCR provider is misconfigured", retryable=False)
            by_artifact_id = {item.id: item for item in artifacts}
            inputs: list[OcrPageInput] = []
            for page in pages:
                if page.page_number not in needs_ocr:
                    continue
                image_artifact = by_artifact_id.get(page.image_artifact_id)
                if image_artifact is None:
                    raise StageExecutionError(
                        f"page {page.page_number} has no image artifact", retryable=False
                    )
                try:
                    image = await self._store.get(image_artifact.object_key)
                except ObjectNotFoundError:
                    raise StageExecutionError(
                        f"page {page.page_number} image is missing", retryable=True
                    ) from None
                inputs.append(
                    OcrPageInput(
                        page_number=page.page_number,
                        width_px=page.width_px,
                        height_px=page.height_px,
                        image=image,
                        content_type="image/png",
                    )
                )
            try:
                ocr_result = await ocr.recognize(
                    OcrRequest(
                        document_id=document.id,
                        document_sha256=document.content_sha256,
                        pages=tuple(inputs),
                        languages=self._config.languages,
                    )
                )
            except OcrProviderError as error:
                raise StageExecutionError(str(error), retryable=error.retryable) from None
            warnings.extend(ocr_result.warnings)
            for ocr_page in ocr_result.pages:
                texts[ocr_page.page_number] = "\n".join(
                    line.text for block in ocr_page.blocks for line in block.lines
                )

        for page in pages:
            text = texts.get(page.page_number, "")
            if not text.strip():
                raise StageExecutionError(
                    f"page {page.page_number} produced no readable text",
                    retryable=False,
                )
            key = artifact_key(
                context.organization_id,
                document.id,
                kind="ocr_text",
                filename=f"page-{page.page_number:04}.txt",
            )
            encoded = text.encode("utf-8")
            metadata = await self._store.put(key, encoded, content_type="text/plain; charset=utf-8")
            artifact = await create_artifact(
                session,
                context,
                document_id=document.id,
                kind=ArtifactKind.OCR_TEXT,
                object_key=key,
                sha256=metadata.sha256,
                size_bytes=len(encoded),
                content_type="text/plain; charset=utf-8",
                produced_by_run_id=run.id,
                produced_by_stage="extracting",
                actor_id=ACTOR,
            )
            page.text_artifact_id = artifact.id
        return texts, warnings

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
        data = _evaluation_input(rows, document)
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


def _evaluation_input(rows: list[ExtractedField], document: Document) -> EvaluationInput:
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
    if document.duplicate_of is not None:
        header["meta.duplicate_of"] = str(document.duplicate_of)
    return EvaluationInput(
        header=header,
        tables={
            table: [cells for _, cells in sorted(by_row.items())]
            for table, by_row in tables.items()
        },
    )
