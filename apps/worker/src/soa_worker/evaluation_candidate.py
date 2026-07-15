"""Read-only execution of a pinned candidate over stored gold source bytes.

This path deliberately does not invoke the normal stage orchestrator: that
orchestrator writes page artifacts, extracted fields, catalog selections, and
review tasks against a business document.  An evaluation must exercise the
same renderer, recognition adapters, extraction provider, normalizers, rules,
confidence policy, and business checks without changing that source document.
Only the evaluation run's checkpoint/report/attestation are durable outputs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_config import SecretStore
from soa_db.artifacts import Artifact, ArtifactKind, ArtifactRepository
from soa_db.documents import Document, DocumentRepository
from soa_db.evaluation_runs import (
    SERVER_ATTESTATION_SCHEMA_VERSION,
    EvaluationRun,
    attestation_digest,
)
from soa_db.extracted_fields import ExtractedField as StoredField
from soa_db.gold_datasets import GoldDocument
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun
from soa_normalize import NormalizationError, normalize
from soa_rules import EvaluationInput, FieldSignal, decide_route, evaluate_rule_set
from soa_storage import ObjectNotFoundError, ObjectStore, sha256_hex
from soa_worker.catalog_business import validate_order_business_data
from soa_worker.evaluation import EvalDocument, EvalPrediction
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    PageInput,
    validate_result_against_request,
)
from soa_worker.provider_router import (
    NATIVE_COVERAGE_THRESHOLD,
    DocumentFacts,
    RoutingPolicy,
    route,
)
from soa_worker.providers import Capability, ProviderInfo, create_provider, provider_info
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextFailure,
    NativeTextProvider,
    NativeTextRequest,
)
from soa_worker.providers.ocr import OcrPageInput, OcrProvider, OcrProviderError, OcrRequest
from soa_worker.rendering import RenderedPage, RenderError, render_document
from soa_worker.run_config import (
    ResolvedRunConfig,
    RunConfigError,
    execution_fingerprint,
    load_resolved_run_config,
    snapshot_fingerprint,
)
from soa_worker.runtime_provenance import (
    extraction_provenance,
    package_version,
    renderer_provenance,
    runtime_fingerprint,
)


class CandidateEvaluationError(ValueError):
    """Display-safe refusal of incomplete or unauthenticated evidence."""

    def __init__(self, safe_message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(safe_message)


@dataclass(frozen=True)
class CandidateEvaluator:
    provider_info: ProviderInfo
    resolved: ResolvedRunConfig
    provider: ExtractionProvider
    run: EvaluationRun
    session: AsyncSession
    context: OrganizationContext
    store: ObjectStore
    gold_by_sha: dict[str, GoldDocument]
    sources_by_sha: dict[str, tuple[Document, Artifact]]

    async def extract(self, document: EvalDocument) -> EvalPrediction:
        gold = self.gold_by_sha.get(document.document_sha256)
        if gold is None:
            raise CandidateEvaluationError("gold document is absent from the immutable manifest")
        prediction, evidence = await _execute_document(self, gold)
        attestation = dict(self.run.attestation or {})
        documents = dict(attestation.get("documents", {}))
        existing = documents.get(document.document_sha256)
        if existing is not None and existing != evidence:
            raise CandidateEvaluationError(
                "runtime evidence changed while the evaluation was being retried"
            )
        documents[document.document_sha256] = evidence
        attestation["documents"] = documents
        attestation["manifest_fingerprint"] = attestation_digest(attestation)
        self.run.attestation = attestation
        await self.session.flush()
        return prediction


def _uuid(raw: object, label: str, *, required: bool = False) -> uuid.UUID | None:
    if raw is None and not required:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError) as error:
        raise CandidateEvaluationError(f"the persisted {label} pin is invalid") from error


def _contract_run(run: EvaluationRun) -> ProcessingRun:
    pins = run.runtime_pins
    if not isinstance(pins, dict) or run.stream_version_id is None:
        raise CandidateEvaluationError("the evaluation has no persisted runtime pins")
    provider_policy = _uuid(
        pins.get("provider_policy_version_id"), "provider policy", required=True
    )
    assert provider_policy is not None
    contract = ProcessingRun(
        organization_id=run.organization_id,
        document_id=uuid.uuid4(),
        run_number=1,
        stream_version_id=run.stream_version_id,
        config_fingerprint=run.candidate_fingerprint,
        instruction_version_id=_uuid(pins.get("instruction_version_id"), "instruction"),
        confidence_policy_version_id=_uuid(
            pins.get("confidence_policy_version_id"), "confidence policy"
        ),
        provider_policy_version_id=provider_policy,
        provider_credential_ref=pins.get("provider_credential_ref"),
        execution_fingerprint=run.execution_fingerprint,
        input_sha256="0" * 64,
        triggered_by="system:evaluation",
    )
    if execution_fingerprint(contract) != run.execution_fingerprint:
        raise CandidateEvaluationError("the persisted evaluation execution fingerprint is invalid")
    return contract


async def build_candidate_evaluator(
    session: AsyncSession,
    context: OrganizationContext,
    run: EvaluationRun,
    documents: list[GoldDocument],
    *,
    store: ObjectStore,
    secret_store: SecretStore,
) -> CandidateEvaluator:
    snapshot = run.candidate_snapshot
    if not isinstance(snapshot, dict):
        raise CandidateEvaluationError("the evaluation has no immutable candidate snapshot")
    if (
        snapshot.get("fingerprint") != run.candidate_fingerprint
        or snapshot_fingerprint(snapshot) != run.candidate_fingerprint
    ):
        raise CandidateEvaluationError("the persisted candidate snapshot fingerprint is invalid")
    contract = _contract_run(run)
    try:
        resolved = await load_resolved_run_config(
            session,
            context,
            contract,
            snapshot=snapshot,
        )
    except RunConfigError as error:
        raise CandidateEvaluationError(str(error)) from error
    if resolved.fingerprint != run.candidate_fingerprint:
        raise CandidateEvaluationError("the resolved candidate fingerprint changed")
    credential_value: str | None = None
    if resolved.credential_reference is not None:
        try:
            credential_value = await secret_store.resolve(resolved.credential_reference)
        except Exception as error:
            raise CandidateEvaluationError(
                "the pinned provider credential cannot be resolved",
                retryable=True,
            ) from error
    runtime: dict[str, Any] = {}
    if resolved.provider_name != "mock":
        runtime = {
            "instructions": resolved.instructions,
            "instruction_reference": resolved.instruction_reference,
            "credential_value": credential_value,
        }
    try:
        provider = create_provider(
            Capability.FIELD_EXTRACTION,
            resolved.provider_name,
            **runtime,
        )
    except Exception as error:
        raise CandidateEvaluationError("the pinned extraction provider is unavailable") from error
    if not isinstance(provider, ExtractionProvider):
        raise CandidateEvaluationError("the pinned provider does not implement field extraction")
    info = provider_info(Capability.FIELD_EXTRACTION, resolved.provider_name)
    if info.data_policy.sends_content_to_third_party and not run.allow_external_provider:
        raise CandidateEvaluationError(
            "the candidate sends content to a third party; explicit evaluation consent is absent"
        )
    gold_by_sha = {document.document_sha256: document for document in documents}
    if len(gold_by_sha) != len(documents):
        raise CandidateEvaluationError("the immutable dataset contains duplicate source hashes")
    if any(document.source_document_id is None for document in documents):
        raise CandidateEvaluationError("a gold document has no source document reference")
    source_ids = [
        document.source_document_id
        for document in documents
        if document.source_document_id is not None
    ]
    sources = await DocumentRepository(session, context).get_many(source_ids)
    artifacts = await ArtifactRepository(session, context).list_for_documents(source_ids)
    sources_by_sha: dict[str, tuple[Document, Artifact]] = {}
    for gold in documents:
        assert gold.source_document_id is not None
        source = sources.get(gold.source_document_id)
        if source is None or source.content_sha256 != gold.document_sha256:
            raise CandidateEvaluationError(
                "gold source document is missing or has a different hash"
            )
        originals = [
            item
            for item in artifacts.get(source.id, [])
            if item.kind == ArtifactKind.ORIGINAL.value
        ]
        if len(originals) != 1:
            raise CandidateEvaluationError("gold source has no unique original artifact")
        original = originals[0]
        if (
            original.sha256 != gold.document_sha256
            or original.size_bytes != source.size_bytes
            or original.content_type != source.content_type
        ):
            raise CandidateEvaluationError(
                "gold source artifact metadata does not match its document"
            )
        sources_by_sha[gold.document_sha256] = (source, original)
    run.attestation = {
        "schema_version": SERVER_ATTESTATION_SCHEMA_VERSION,
        "execution_mode": "server",
        "candidate_fingerprint": run.candidate_fingerprint,
        "execution_fingerprint": run.execution_fingerprint,
        "dataset_version_id": str(run.dataset_version_id),
        "provider": resolved.provider_name,
        "external_provider_consent": run.allow_external_provider,
        "documents": dict((run.attestation or {}).get("documents", {})),
    }
    run.attestation["manifest_fingerprint"] = attestation_digest(run.attestation)
    return CandidateEvaluator(
        provider_info=info,
        resolved=resolved,
        provider=provider,
        run=run,
        session=session,
        context=context,
        store=store,
        gold_by_sha=gold_by_sha,
        sources_by_sha=sources_by_sha,
    )


async def _source(
    evaluator: CandidateEvaluator, gold: GoldDocument
) -> tuple[Document, Artifact, bytes]:
    source_entry = evaluator.sources_by_sha.get(gold.document_sha256)
    if source_entry is None:
        raise CandidateEvaluationError("gold source is absent from the authenticated manifest")
    source, original = source_entry
    try:
        data = await evaluator.store.get(original.object_key)
    except ObjectNotFoundError:
        raise CandidateEvaluationError(
            "gold source bytes are missing from object storage", retryable=True
        ) from None
    if len(data) != original.size_bytes or sha256_hex(data) != gold.document_sha256:
        raise CandidateEvaluationError("gold source bytes failed their immutable hash check")
    return source, original, data


async def _recognize(
    evaluator: CandidateEvaluator,
    source: Document,
    data: bytes,
    pages: list[RenderedPage],
) -> tuple[dict[int, str], list[dict[str, Any]], int]:
    texts: dict[int, str] = {}
    provenance: list[dict[str, Any]] = []
    needs_ocr = {page.page_number for page in pages}
    if source.content_type == "application/pdf":
        native = create_provider(Capability.NATIVE_TEXT, "pdfium-native-text")
        if not isinstance(native, NativeTextProvider):
            raise CandidateEvaluationError("native text provider is misconfigured")
        try:
            native_result = await native.read(
                NativeTextRequest(
                    document_id=source.id,
                    document_sha256=source.content_sha256,
                    content_type=source.content_type,
                    data=data,
                    max_pages=len(pages),
                )
            )
        except NativeTextError as error:
            if error.failure in (NativeTextFailure.ENCRYPTED, NativeTextFailure.CORRUPT):
                raise CandidateEvaluationError(str(error)) from None
            if error.failure is NativeTextFailure.UNAVAILABLE:
                raise CandidateEvaluationError(str(error), retryable=error.retryable) from None
            provenance.append(
                {
                    "provider": native.name,
                    "model": f"pdfium {package_version('pypdfium2')}",
                    "outcome": "fallback",
                    "failure": error.failure.value,
                }
            )
        else:
            accepted: list[int] = []
            for result_page in native_result.pages:
                if result_page.coverage >= NATIVE_COVERAGE_THRESHOLD:
                    texts[result_page.page_number] = "\n".join(
                        span.text for span in result_page.spans
                    )
                    needs_ocr.discard(result_page.page_number)
                    accepted.append(result_page.page_number)
            provenance.append(
                {
                    "provider": native_result.provider,
                    "model": native_result.model,
                    "outcome": "used",
                    "accepted_pages": accepted,
                }
            )

    ocr_cost = 0
    if needs_ocr:
        try:
            decision = route(
                Capability.OCR,
                policy=RoutingPolicy(local_only=True),
                facts=DocumentFacts(language=evaluator.resolved.pipeline.languages[0]),
            )
            ocr = create_provider(Capability.OCR, decision.provider.name)
        except Exception as error:
            raise CandidateEvaluationError(
                f"OCR is required but no local provider is available ({type(error).__name__})"
            ) from None
        if not isinstance(ocr, OcrProvider):
            raise CandidateEvaluationError("OCR provider is misconfigured")
        by_number = {page.page_number: page for page in pages}
        try:
            result = await ocr.recognize(
                OcrRequest(
                    document_id=source.id,
                    document_sha256=source.content_sha256,
                    pages=tuple(
                        OcrPageInput(
                            page_number=page_number,
                            width_px=by_number[page_number].width_px,
                            height_px=by_number[page_number].height_px,
                            image=by_number[page_number].image_png,
                            content_type="image/png",
                        )
                        for page_number in sorted(needs_ocr)
                    ),
                    languages=evaluator.resolved.pipeline.languages,
                )
            )
        except OcrProviderError as error:
            raise CandidateEvaluationError(str(error), retryable=error.retryable) from None
        ocr_cost = result.cost_cents
        recognized: list[int] = []
        for ocr_page in result.pages:
            texts[ocr_page.page_number] = "\n".join(
                line.text for block in ocr_page.blocks for line in block.lines
            )
            recognized.append(ocr_page.page_number)
        provenance.append(
            {
                "provider": result.provider,
                "model": result.model,
                "outcome": "used",
                "pages": recognized,
            }
        )
    for page in pages:
        if not texts.get(page.page_number, "").strip():
            raise CandidateEvaluationError(f"page {page.page_number} produced no readable text")
    return texts, provenance, ocr_cost


def _stored_fields(
    evaluator: CandidateEvaluator,
    source: Document,
    result: Any,
) -> list[StoredField]:
    config = evaluator.resolved.pipeline
    specs = {spec.key: spec for spec in config.field_specs}
    seen: set[tuple[str, int | None]] = set()
    rows: list[StoredField] = []
    for field in result.fields:
        identity = (field.field_key, field.row_index)
        if identity in seen:
            raise CandidateEvaluationError("the provider returned a duplicate field identity")
        seen.add(identity)
        spec = specs.get(field.field_key)
        normalized: Any | None = None
        normalization_error: str | None = None
        if field.raw_value is not None and spec is not None:
            kind = config.normalizer_for(spec)
            if kind is not None:
                try:
                    normalized = normalize(
                        kind,
                        field.raw_value,
                        context=config.normalization,
                        enum_values=spec.enum_values,
                    )
                except NormalizationError:
                    normalization_error = "normalization failed"
        rows.append(
            StoredField(
                organization_id=evaluator.context.organization_id,
                document_id=source.id,
                run_id=evaluator.run.id,
                field_key=field.field_key,
                row_index=field.row_index,
                raw_value=field.raw_value,
                normalized_value=normalized,
                normalization_error=normalization_error,
                confidence=field.confidence,
                provider=result.provider,
                provider_model=result.model,
                instruction_reference=result.instruction_reference,
                config_fingerprint=evaluator.run.candidate_fingerprint,
                execution_fingerprint=evaluator.run.execution_fingerprint,
                evidence_json=[
                    {
                        "page_number": span.page_number,
                        "certainty": "region",
                        "polygon": [[x, y] for x, y in span.polygon],
                        "quote": span.quote,
                    }
                    for span in field.evidence
                ],
                candidates_json=[
                    {"raw_value": item.raw_value, "confidence": item.confidence}
                    for item in field.candidates
                ],
            )
        )
    required_headers = {
        spec.key
        for spec in config.field_specs
        if spec.field_type != "table" and "." not in spec.key
    }
    returned_headers = {row.field_key for row in rows if row.row_index is None}
    missing = sorted(required_headers - returned_headers)
    if missing:
        raise CandidateEvaluationError(
            "the provider omitted required explicit field results: " + ", ".join(missing)
        )
    return rows


def _value(row: StoredField) -> Any:
    return row.normalized_value if row.normalized_value is not None else row.raw_value


def _rule_input(rows: list[StoredField]) -> EvaluationInput:
    header: dict[str, Any] = {}
    tables: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        value = _value(row)
        if row.row_index is None:
            header[row.field_key] = value
        else:
            table = row.field_key.partition(".")[0]
            tables.setdefault(table, {}).setdefault(row.row_index, {})[row.field_key] = value
    return EvaluationInput(
        header=header,
        tables={
            table: [cells for _, cells in sorted(by_row.items())]
            for table, by_row in tables.items()
        },
    )


def _score_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict) and "amount" in value:
        return str(value["amount"])
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _execute_document(
    evaluator: CandidateEvaluator, gold: GoldDocument
) -> tuple[EvalPrediction, dict[str, Any]]:
    source, original, data = await _source(evaluator, gold)
    try:
        pages = await render_document(
            data,
            content_type=source.content_type,
            limits=evaluator.resolved.pipeline.render_limits,
        )
    except RenderError as error:
        raise CandidateEvaluationError(str(error), retryable=error.retryable) from None
    texts: dict[int, str] = {}
    recognition: list[dict[str, Any]] = []
    recognition_cost = 0
    if evaluator.provider.name != "mock":
        texts, recognition, recognition_cost = await _recognize(evaluator, source, data, pages)
    request = ExtractionRequest(
        document_id=source.id,
        document_sha256=source.content_sha256,
        content_type=source.content_type,
        pages=tuple(
            PageInput(
                page_number=page.page_number,
                width_px=page.width_px,
                height_px=page.height_px,
                text=texts.get(page.page_number),
            )
            for page in pages
        ),
        fields=evaluator.resolved.pipeline.field_specs,
    )
    try:
        result = await evaluator.provider.extract(request)
    except ExtractionProviderError as error:
        raise CandidateEvaluationError(str(error), retryable=error.retryable) from None
    violations = validate_result_against_request(request, result)
    if violations:
        raise CandidateEvaluationError(
            "the extraction provider violated its contract: " + "; ".join(violations[:5])
        )
    rows = _stored_fields(evaluator, source, result)
    evaluation = evaluate_rule_set(evaluator.resolved.pipeline.rules, _rule_input(rows))
    decision = decide_route(
        [
            FieldSignal(
                field_key=row.field_key,
                criticality=evaluator.resolved.pipeline.criticality.get(row.field_key, "standard"),
                present=row.raw_value is not None,
                confidence=row.confidence,
                has_evidence=bool(row.evidence_json),
                row_index=row.row_index,
                top_candidate_confidence=max(
                    (candidate.confidence for candidate in row.candidate_readings()),
                    default=None,
                ),
            )
            for row in rows
        ],
        evaluation,
        evaluator.resolved.pipeline.confidence_policy,
    )
    evaluation_document = Document(
        id=source.id,
        organization_id=source.organization_id,
        stream_id=evaluator.run.stream_id,
        state=source.state,
        source_channel=source.source_channel,
        original_filename=source.original_filename,
        content_sha256=source.content_sha256,
        size_bytes=source.size_bytes,
        content_type=source.content_type,
        priority=source.priority,
        received_at=source.received_at,
        source_metadata={},
        duplicate_of=source.duplicate_of,
    )
    business = await validate_order_business_data(
        evaluator.session,
        evaluator.context,
        _contract_run(evaluator.run),
        evaluation_document,
        rows,
        stream_config=evaluator.resolved.pipeline.stream_config,
        record_selections=False,
    )
    approved = decision.approved and not business.findings
    fields: dict[str, str | None] = {}
    line_rows: dict[int, dict[str, str | None]] = {}
    for row in rows:
        value = _score_value(_value(row))
        if row.row_index is None:
            fields[row.field_key] = value
        elif row.field_key.startswith("lines."):
            line_rows.setdefault(row.row_index, {})[row.field_key.removeprefix("lines.")] = value
    provenance = {
        "schema_version": 1,
        "contract_fingerprint": evaluator.run.execution_fingerprint,
        "evaluation_runtime": {
            "worker": package_version("soa-worker"),
            "normalization": package_version("soa-normalize"),
            "rules": package_version("soa-rules"),
        },
        "rendering": renderer_provenance(source.content_type),
        "recognition": recognition,
        "extraction": extraction_provenance(
            evaluator.provider,
            name=result.provider,
            model=result.model,
        ),
        "business_catalog_versions": {
            key: list(value) for key, value in sorted(business.catalog_versions.items())
        },
    }
    actual_fingerprint = runtime_fingerprint(provenance)
    total_cost = recognition_cost + result.cost_cents
    evidence = {
        "document_sha256": gold.document_sha256,
        "source_document_id": str(source.id),
        "source_artifact_id": str(original.id),
        "runtime_fingerprint": actual_fingerprint,
        "runtime_provenance": provenance,
        "provider": result.provider,
        "model": result.model,
        "instruction_reference": result.instruction_reference,
        "route": "approved" if approved else "review_required",
        "cost_cents": total_cost,
    }
    return (
        EvalPrediction(
            fields=fields,
            lines=tuple(line_rows[index] for index in sorted(line_rows)),
            predicted_class="purchase_order",
            cost_cents=total_cost,
            would_auto_approve=approved,
        ),
        evidence,
    )


__all__ = [
    "CandidateEvaluationError",
    "CandidateEvaluator",
    "build_candidate_evaluator",
]
