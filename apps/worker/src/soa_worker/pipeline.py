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
from soa_rules.baseline import BASELINE_VERSION, baseline_sales_order_rules
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
from soa_worker.rendering import RenderError, RenderLimits, render_document

ACTOR = "system:pipeline"

#: Default normalizer per schema field type; per-key overrides win.
_TYPE_NORMALIZERS = {
    "text": "trim",
    "date": "date_iso",
    "money": "money",
    "number": "decimal",
    "boolean": "boolean",
    "enum": "enum",
}


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
        return override or _TYPE_NORMALIZERS.get(spec.field_type)


def canonical_sales_order_config() -> PipelineConfig:
    """The platform's stock sales-order pipeline configuration —
    the PRC-006 fixture schema + PRC-010 baseline rules + PRC-011
    baseline policy, US-locale normalization."""
    return PipelineConfig(
        field_specs=SYNTHETIC_SALES_ORDER_FIELD_SPECS,
        criticality={
            "po_number": "critical",
            "order_date": "critical",
            "requested_delivery_date": "standard",
            "customer_name": "standard",
            "currency": "standard",
            "total_amount": "standard",
            "delivery_terms": "informational",
            "lines.sku": "standard",
            "lines.description": "standard",
            "lines.quantity": "standard",
            "lines.unit_price": "standard",
            "lines.line_total": "standard",
        },
        rules=baseline_sales_order_rules(),
        rules_version=BASELINE_VERSION,
        normalization=NormalizationContext(locale="en-US", currency="USD"),
        normalizer_overrides={"po_number": "identifier", "lines.sku": "identifier"},
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
        request = ExtractionRequest(
            document_id=document.id,
            document_sha256=document.content_sha256,
            content_type=document.content_type,
            pages=tuple(
                PageInput(page_number=p.page_number, width_px=p.width_px, height_px=p.height_px)
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
        summary["warnings"] = list(result.warnings)
        return StageOutcome(
            output_summary=summary, cost_cents=result.cost_cents, provider=result.provider
        )

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
