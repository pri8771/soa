"""Execute and checkpoint a stored evaluation run."""

import json
import time
import uuid
from typing import Any

from soa_config import SecretStore
from soa_db.evaluation_runs import (
    CALLER_SUBMITTED_EVIDENCE_SOURCE,
    PROMOTABLE_EVIDENCE_SOURCE,
    EvaluationExecutionMode,
    EvaluationRun,
    EvaluationRunRepository,
    EvaluationRunState,
    finish_evaluation_run,
    is_promotable_server_evidence,
    start_evaluation_run,
)
from soa_db.gold_datasets import GoldDocumentRepository
from soa_db.repository import OrganizationContext
from soa_storage import ObjectStore
from soa_worker.evaluation import (
    EVALUATION_METRIC_SCHEMA_VERSION,
    CohortMetrics,
    EvalDocument,
    EvalPrediction,
    EvaluationReport,
    EvaluationState,
    FieldScore,
    run_evaluation,
)
from soa_worker.evaluation_candidate import build_candidate_evaluator
from soa_worker.evaluation_gate import (
    Finding,
    GatePolicy,
    GateResult,
    evaluate_promotion_gate,
)

PROMOTION_POLICY = GatePolicy(
    critical_fields=("po_number", "customer_name", "lines.sku", "lines.quantity")
)


class BaselineCompatibilityError(ValueError):
    """A baseline cannot be compared safely with the candidate report."""


def _prediction(raw: dict[str, Any]) -> EvalPrediction:
    return EvalPrediction(
        fields=dict(raw.get("fields", {})),
        lines=tuple(dict(line) for line in raw.get("lines", [])),
        predicted_class=raw.get("predicted_class"),
        cost_cents=int(raw.get("cost_cents", 0)),
        would_auto_approve=bool(raw.get("would_auto_approve", False)),
    )


def _gate_json(result: GateResult, *, baseline_compatible: bool | None) -> dict[str, Any]:
    findings = [vars(item) for item in result.findings]
    findings.append(
        {
            "kind": "non_promotable_evidence_source",
            "detail": (
                "predictions submitted by the caller are simulation-only and cannot satisfy "
                "the stream publication gate"
            ),
            "waivable": False,
        }
    )
    return {
        # ``passed`` is the sole field consumed by historical publication
        # code, so caller-supplied predictions always fail closed here.
        "passed": False,
        "simulation_passed": result.passed,
        "promotion_eligible": False,
        "evidence_source": CALLER_SUBMITTED_EVIDENCE_SOURCE,
        "metric_schema_version": EVALUATION_METRIC_SCHEMA_VERSION,
        "baseline_compatible": baseline_compatible,
        "field_diffs": [vars(item) for item in result.field_diffs],
        "cohort_diffs": [vars(item) for item in result.cohort_diffs],
        "findings": findings,
    }


def _server_gate_json(
    result: GateResult,
    *,
    run: EvaluationRun,
    report: EvaluationReport,
    state: EvaluationState,
    baseline_compatible: bool | None,
) -> dict[str, Any]:
    attestation = run.attestation or {}
    documents = attestation.get("documents")
    catalog_snapshots: set[str] = set()
    catalog_shape_valid = isinstance(documents, dict)
    if isinstance(documents, dict):
        for evidence in documents.values():
            provenance = evidence.get("runtime_provenance") if isinstance(evidence, dict) else None
            if not isinstance(provenance, dict):
                catalog_shape_valid = False
                continue
            catalog_snapshots.add(
                json.dumps(
                    provenance.get("business_catalog_versions", {}),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    catalogs_consistent = catalog_shape_valid and len(catalog_snapshots) <= 1
    complete = (
        isinstance(documents, dict)
        and set(documents) == set(state.scores)
        and catalogs_consistent
        and report.documents_failed == 0
        and report.documents_scored > 0
        and attestation.get("manifest_fingerprint") is not None
    )
    findings = [vars(item) for item in result.findings]
    if not complete:
        findings.append(
            {
                "kind": "incomplete_server_attestation",
                "detail": (
                    "server execution did not attest every immutable dataset document; "
                    "partial evaluation output cannot satisfy the publication gate"
                ),
                "waivable": False,
            }
        )
    if not catalogs_consistent:
        findings.append(
            {
                "kind": "inconsistent_catalog_snapshot",
                "detail": (
                    "catalog versions changed across evaluated documents; rerun against one "
                    "consistent business-data snapshot"
                ),
                "waivable": False,
            }
        )
    passed = result.passed and complete
    return {
        "passed": passed,
        "simulation_passed": None,
        "promotion_eligible": passed,
        "evidence_source": PROMOTABLE_EVIDENCE_SOURCE,
        "metric_schema_version": EVALUATION_METRIC_SCHEMA_VERSION,
        "baseline_compatible": baseline_compatible,
        "execution_fingerprint": run.execution_fingerprint,
        "attestation_fingerprint": attestation.get("manifest_fingerprint"),
        "field_diffs": [vars(item) for item in result.field_diffs],
        "cohort_diffs": [vars(item) for item in result.cohort_diffs],
        "findings": findings,
    }


def _report_from_json(raw: dict[str, Any]) -> EvaluationReport:
    if raw.get("metric_schema_version") != EVALUATION_METRIC_SCHEMA_VERSION:
        raise BaselineCompatibilityError("baseline metric schema is missing or incompatible")
    try:
        return EvaluationReport(
            **{
                **raw,
                "by_field": {key: FieldScore(**value) for key, value in raw["by_field"].items()},
                "by_cohort": {
                    key: CohortMetrics(**value) for key, value in raw["by_cohort"].items()
                },
            }
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BaselineCompatibilityError(
            "baseline report has an incompatible metric shape"
        ) from error


def _compatible_baseline_report(
    run: EvaluationRun,
    baseline: EvaluationRun | None,
    candidate: EvaluationReport,
) -> EvaluationReport:
    # The repository used by the caller is organization-scoped.  A missing
    # row therefore also covers a cross-tenant baseline ID without revealing
    # whether that ID exists elsewhere.
    if baseline is None:
        raise BaselineCompatibilityError("baseline is unavailable in this organization")
    if baseline.organization_id != run.organization_id:
        raise BaselineCompatibilityError("baseline is unavailable in this organization")
    if baseline.id == run.id:
        raise BaselineCompatibilityError("an evaluation cannot use itself as its baseline")
    if baseline.state != EvaluationRunState.SUCCEEDED or baseline.report is None:
        raise BaselineCompatibilityError("baseline is not a completed evaluation")
    if baseline.stream_id != run.stream_id:
        raise BaselineCompatibilityError("baseline belongs to a different stream")
    if baseline.dataset_version_id != run.dataset_version_id:
        raise BaselineCompatibilityError("baseline uses a different dataset version")

    report = _report_from_json(baseline.report)
    if report.by_split != candidate.by_split:
        raise BaselineCompatibilityError("baseline cohort membership or document counts differ")
    if set(report.by_cohort) != set(candidate.by_cohort):
        raise BaselineCompatibilityError("baseline cohort schema differs")
    if any(
        report.by_cohort[key].documents != candidate.by_cohort[key].documents
        for key in report.by_cohort
    ):
        raise BaselineCompatibilityError("baseline cohort document counts differ")
    if set(report.by_field) != set(candidate.by_field):
        raise BaselineCompatibilityError("baseline field metric schema differs")
    return report


async def execute_evaluation(
    session: Any,
    context: OrganizationContext,
    run_id: uuid.UUID,
    *,
    store: ObjectStore | None = None,
    secret_store: SecretStore | None = None,
) -> None:
    repo = EvaluationRunRepository(session, context)
    run = await repo.get(run_id)
    if run is None:
        raise ValueError("evaluation run does not exist")
    if run.state == EvaluationRunState.SUCCEEDED:
        return
    await start_evaluation_run(run)
    documents = await GoldDocumentRepository(session, context).list_for_version(
        run.dataset_version_id
    )
    eval_documents = [
        EvalDocument(
            document_sha256=document.document_sha256,
            split=document.split,
            ground_truth=document.ground_truth,
            expected_class=document.expected_class,
        )
        for document in documents
    ]

    state = EvaluationState.from_dict(run.checkpoint) if run.checkpoint else EvaluationState()
    try:
        provider = None
        if run.execution_mode == EvaluationExecutionMode.SERVER.value:
            if store is None or secret_store is None:
                raise ValueError("server evaluation runtime dependencies are unavailable")
            evaluator = await build_candidate_evaluator(
                session,
                context,
                run,
                documents,
                store=store,
                secret_store=secret_store,
            )
            extract = evaluator.extract
            provider = evaluator.provider_info
        else:

            async def extract(document: EvalDocument) -> EvalPrediction:
                raw = run.predictions.get(document.document_sha256)
                if not isinstance(raw, dict):
                    raise ValueError("candidate produced no prediction for this document")
                return _prediction(raw)

        report, state = await run_evaluation(
            eval_documents,
            extract,
            provider_info=provider,
            allow_external=run.allow_external_provider,
            state=state,
            clock=time.perf_counter,
        )
    except Exception as error:
        if getattr(error, "retryable", False) is True:
            # A retry starts from this exact immutable contract and skips
            # documents already scored. Commit only the safe checkpoint and
            # content-free attestation before returning the error to the queue.
            run.checkpoint = state.to_dict()
            await session.flush()
            await session.commit()
        raise
    report_json = json.loads(report.to_json())
    gate_result: GateResult
    baseline_compatible: bool | None = None
    if run.baseline_run_id is None:
        gate_result = evaluate_promotion_gate(report, policy=PROMOTION_POLICY)
    else:
        baseline = await repo.get(run.baseline_run_id)
        try:
            if run.execution_mode == EvaluationExecutionMode.SERVER.value and (
                baseline is None or not is_promotable_server_evidence(baseline)
            ):
                raise BaselineCompatibilityError(
                    "baseline is not complete attested server-executed evidence"
                )
            baseline_report = _compatible_baseline_report(run, baseline, report)
        except BaselineCompatibilityError as error:
            baseline_compatible = False
            absolute = evaluate_promotion_gate(report, policy=PROMOTION_POLICY)
            gate_result = GateResult(
                field_diffs=(),
                cohort_diffs=(),
                findings=(
                    *absolute.findings,
                    Finding(kind="baseline_incompatible", detail=str(error), waivable=False),
                ),
            )
        else:
            baseline_compatible = True
            gate_result = evaluate_promotion_gate(
                report,
                current=baseline_report,
                policy=PROMOTION_POLICY,
            )
    if run.execution_mode == EvaluationExecutionMode.SERVER.value:
        gate_json = _server_gate_json(
            gate_result,
            run=run,
            report=report,
            state=state,
            baseline_compatible=baseline_compatible,
        )
    else:
        gate_json = _gate_json(gate_result, baseline_compatible=baseline_compatible)
    await finish_evaluation_run(
        run,
        report=report_json,
        checkpoint=state.to_dict(),
        gate_result=gate_json,
    )
