"""Execute and checkpoint a stored evaluation run."""

import json
import uuid
from typing import Any

from soa_db.evaluation_runs import (
    EvaluationRunRepository,
    EvaluationRunState,
    finish_evaluation_run,
    start_evaluation_run,
)
from soa_db.gold_datasets import GoldDocumentRepository
from soa_db.repository import OrganizationContext
from soa_worker.evaluation import EvalDocument, EvalPrediction, EvaluationState, run_evaluation
from soa_worker.evaluation_gate import GatePolicy, compare_reports


def _prediction(raw: dict[str, Any]) -> EvalPrediction:
    return EvalPrediction(
        fields=dict(raw.get("fields", {})),
        lines=tuple(dict(line) for line in raw.get("lines", [])),
        predicted_class=raw.get("predicted_class"),
        cost_cents=int(raw.get("cost_cents", 0)),
        would_auto_approve=bool(raw.get("would_auto_approve", False)),
    )


def _gate_json(result: Any) -> dict[str, Any]:
    return {
        "passed": result.passed,
        "field_diffs": [vars(item) for item in result.field_diffs],
        "cohort_diffs": [vars(item) for item in result.cohort_diffs],
        "findings": [vars(item) for item in result.findings],
    }


async def execute_evaluation(session: Any, context: OrganizationContext, run_id: uuid.UUID) -> None:
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

    async def extract(document: EvalDocument) -> EvalPrediction:
        raw = run.predictions.get(document.document_sha256)
        if not isinstance(raw, dict):
            raise ValueError("candidate produced no prediction for this document")
        return _prediction(raw)

    state = EvaluationState.from_dict(run.checkpoint) if run.checkpoint else EvaluationState()
    report, state = await run_evaluation(eval_documents, extract, state=state)
    report_json = json.loads(report.to_json())
    gate: dict[str, Any]
    if run.baseline_run_id is None:
        gate = {
            "passed": report.documents_failed == 0,
            "field_diffs": [],
            "cohort_diffs": [],
            "findings": (
                []
                if report.documents_failed == 0
                else [
                    {
                        "kind": "evaluation_errors",
                        "detail": "one or more documents failed",
                        "waivable": False,
                    }
                ]
            ),
        }
    else:
        baseline = await repo.get(run.baseline_run_id)
        if baseline is None or baseline.report is None:
            raise ValueError("evaluation baseline is missing or incomplete")
        from soa_worker.evaluation import CohortMetrics, EvaluationReport, FieldScore

        raw = baseline.report
        baseline_report = EvaluationReport(
            **{
                **raw,
                "by_field": {key: FieldScore(**value) for key, value in raw["by_field"].items()},
                "by_cohort": {
                    key: CohortMetrics(**value) for key, value in raw["by_cohort"].items()
                },
            }
        )
        gate = _gate_json(
            compare_reports(
                baseline_report,
                report,
                GatePolicy(
                    critical_fields=("po_number", "customer_name", "lines.sku", "lines.quantity")
                ),
            )
        )
    await finish_evaluation_run(
        run,
        report=report_json,
        checkpoint=state.to_dict(),
        gate_result=gate,
    )
