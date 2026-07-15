"""Command-line front end for the AIO-016 evaluation runner.

Without arguments it runs the checked-in synthetic smoke cohort. Real
promotion runs pass a gold dataset and candidate predictions as JSON; this
keeps ordinary PR evaluation offline and makes external provider calls an
explicit upstream action rather than a side effect of ``make eval``.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from soa_worker.evaluation import (
    EvalDocument,
    EvalPrediction,
    EvaluationState,
    run_evaluation,
)

_SMOKE_DOCUMENTS = [
    EvalDocument(
        document_sha256="a" * 64,
        split="test",
        ground_truth={
            "fields": {"po_number": "PO-100042", "currency": "USD"},
            "lines": [{"sku": "WID-100", "quantity": "10"}],
        },
        expected_class="sales_order",
    )
]
_SMOKE_PREDICTIONS = {
    "a" * 64: EvalPrediction(
        fields={"po_number": "PO-100042", "currency": "USD"},
        lines=({"sku": "WID-100", "quantity": "10"},),
        predicted_class="sales_order",
    )
}


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _documents(raw: list[dict[str, Any]]) -> list[EvalDocument]:
    return [
        EvalDocument(
            document_sha256=str(item["document_sha256"]),
            split=str(item["split"]),
            ground_truth=dict(item["ground_truth"]),
            expected_class=(
                str(item["expected_class"]) if item.get("expected_class") is not None else None
            ),
        )
        for item in raw
    ]


def _predictions(raw: dict[str, dict[str, Any]]) -> dict[str, EvalPrediction]:
    return {
        sha: EvalPrediction(
            fields=dict(item.get("fields", {})),
            lines=tuple(dict(line) for line in item.get("lines", [])),
            predicted_class=item.get("predicted_class"),
            cost_cents=int(item.get("cost_cents", 0)),
            would_auto_approve=bool(item.get("would_auto_approve", False)),
        )
        for sha, item in raw.items()
    }


async def _run(args: argparse.Namespace) -> int:
    if bool(args.dataset) != bool(args.predictions):
        raise SystemExit("--dataset and --predictions must be supplied together")
    documents = _SMOKE_DOCUMENTS
    predictions = _SMOKE_PREDICTIONS
    if args.dataset:
        documents = _documents(_read_json(args.dataset))
        predictions = _predictions(_read_json(args.predictions))
    state = (
        EvaluationState.from_dict(_read_json(args.state))
        if args.state and Path(args.state).exists()
        else EvaluationState()
    )

    async def extract(document: EvalDocument) -> EvalPrediction:
        try:
            return predictions[document.document_sha256]
        except KeyError:
            raise ValueError("candidate produced no prediction for this document") from None

    report, final_state = await run_evaluation(documents, extract, state=state)
    rendered = report.to_json()
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    if args.state:
        Path(args.state).write_text(
            json.dumps(final_state.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 1 if report.documents_failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Score candidate predictions against gold data")
    parser.add_argument("--dataset", help="gold dataset JSON (list of EvalDocument objects)")
    parser.add_argument("--predictions", help="candidate predictions JSON keyed by document sha")
    parser.add_argument("--state", help="optional resumable checkpoint JSON")
    parser.add_argument("--output", help="optional report output path")
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
