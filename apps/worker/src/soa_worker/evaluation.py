"""Evaluation runner (AIO-016).

Scores a candidate extraction configuration against gold ground truth
(AIO-015). The runner is deliberately dumb about WHERE predictions come
from — the caller supplies an async ``extract_fn`` — and strict about
everything else:

- **repeatable** — documents are processed in stable (sha) order and
  scoring is pure; the same predictions against the same gold always
  produce the same report;
- **resumable** — the runner takes and returns a serialisable
  :class:`EvaluationState`; already-scored documents are never re-run,
  so a crashed evaluation continues where it stopped instead of paying
  for the whole dataset again;
- **no external calls by default** — when the candidate's provider
  metadata says content leaves the deployment, the runner REFUSES
  unless the caller explicitly passes ``allow_external=True``: an
  ordinary test/CI run can never silently ship gold documents to a
  vendor;
- **errors are data** — a document whose extraction fails is recorded
  with its safe message and the run continues; the report says exactly
  how many documents scored and how many failed.

Metrics per field: EXACT (trimmed string equality, with absent-matches-
absent) and NORMALIZED (casefold, whitespace collapse, and numeric
equivalence so "12.50" == "12.5"). Line items align by row index and
score per cell. Latency and cost are accounted per document and
aggregated.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from soa_worker.providers.capabilities import ProviderInfo


class EvaluationRefusedError(Exception):
    pass


@dataclass(frozen=True)
class EvalDocument:
    """One gold document to score (adapt from soa_db GoldDocument)."""

    document_sha256: str
    split: str
    ground_truth: dict[str, Any]
    expected_class: str | None = None


@dataclass(frozen=True)
class EvalPrediction:
    """What the candidate configuration produced for one document."""

    fields: dict[str, str | None]
    lines: tuple[dict[str, str | None], ...] = ()
    predicted_class: str | None = None
    cost_cents: int = 0
    #: Whether the candidate configuration would have AUTO-APPROVED this
    #: document (no human review) — the caller computes it from the
    #: candidate's confidence policy. Feeds the false-auto-approval gate.
    would_auto_approve: bool = False


ExtractFn = Callable[[EvalDocument], Awaitable[EvalPrediction]]
Clock = Callable[[], float]


def _normalize(value: str) -> str:
    collapsed = " ".join(value.split()).casefold()
    try:
        return str(Decimal(collapsed).normalize())
    except InvalidOperation:
        return collapsed


def _matches(expected: str | None, predicted: str | None, *, normalized: bool) -> bool:
    if expected is None or predicted is None:
        return expected is None and predicted is None
    if normalized:
        return _normalize(expected) == _normalize(predicted)
    return expected.strip() == predicted.strip()


@dataclass(frozen=True)
class FieldScore:
    total: int = 0
    exact: int = 0
    normalized: int = 0

    def plus(self, *, exact: bool, normalized: bool) -> "FieldScore":
        return FieldScore(
            total=self.total + 1,
            exact=self.exact + (1 if exact else 0),
            normalized=self.normalized + (1 if normalized else 0),
        )


@dataclass(frozen=True)
class DocumentScore:
    document_sha256: str
    split: str
    fields_total: int
    fields_exact: int
    fields_normalized: int
    line_cells_total: int
    line_cells_exact: int
    lines_expected: int
    lines_predicted: int
    class_correct: bool | None
    latency_ms: float
    cost_cents: int
    auto_approved: bool = False
    #: Field KEYS that failed even normalized matching (never values).
    wrong_fields: tuple[str, ...] = ()


def score_document(
    document: EvalDocument,
    prediction: EvalPrediction,
    *,
    latency_ms: float = 0.0,
) -> tuple[DocumentScore, dict[str, FieldScore]]:
    """Pure scoring of one document; returns the score plus the
    per-field breakdown contribution."""
    by_field: dict[str, FieldScore] = {}
    fields_exact = 0
    fields_normalized = 0
    expected_fields: dict[str, Any] = document.ground_truth.get("fields", {})
    for key, expected in expected_fields.items():
        predicted = prediction.fields.get(key)
        exact = _matches(expected, predicted, normalized=False)
        normalized = exact or _matches(expected, predicted, normalized=True)
        fields_exact += 1 if exact else 0
        fields_normalized += 1 if normalized else 0
        by_field[key] = by_field.get(key, FieldScore()).plus(exact=exact, normalized=normalized)

    expected_lines: list[dict[str, Any]] = document.ground_truth.get("lines", []) or []
    cells_total = 0
    cells_exact = 0
    for index, expected_row in enumerate(expected_lines):
        predicted_row: dict[str, str | None] = (
            prediction.lines[index] if index < len(prediction.lines) else {}
        )
        for key, expected in expected_row.items():
            cells_total += 1
            if _matches(expected, predicted_row.get(key), normalized=False):
                cells_exact += 1

    class_correct: bool | None = None
    if document.expected_class is not None:
        class_correct = prediction.predicted_class == document.expected_class

    wrong_fields = tuple(
        sorted(
            key
            for key, expected in expected_fields.items()
            if not (
                _matches(expected, prediction.fields.get(key), normalized=False)
                or _matches(expected, prediction.fields.get(key), normalized=True)
            )
        )
    )

    return (
        DocumentScore(
            document_sha256=document.document_sha256,
            split=document.split,
            fields_total=len(expected_fields),
            fields_exact=fields_exact,
            fields_normalized=fields_normalized,
            line_cells_total=cells_total,
            line_cells_exact=cells_exact,
            lines_expected=len(expected_lines),
            lines_predicted=len(prediction.lines),
            class_correct=class_correct,
            latency_ms=latency_ms,
            cost_cents=prediction.cost_cents,
            auto_approved=prediction.would_auto_approve,
            wrong_fields=wrong_fields,
        ),
        by_field,
    )


@dataclass
class EvaluationState:
    """Serialisable checkpoint: everything scored or failed so far."""

    scores: dict[str, DocumentScore] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    by_field: dict[str, FieldScore] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": {sha: vars(score) for sha, score in self.scores.items()},
            "errors": dict(self.errors),
            "by_field": {key: vars(score) for key, score in self.by_field.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationState":
        return cls(
            scores={
                sha: DocumentScore(**{**raw, "wrong_fields": tuple(raw.get("wrong_fields", ()))})
                for sha, raw in data.get("scores", {}).items()
            },
            errors=dict(data.get("errors", {})),
            by_field={key: FieldScore(**raw) for key, raw in data.get("by_field", {}).items()},
        )


@dataclass(frozen=True)
class CohortMetrics:
    """Aggregate rates for one cohort (an evaluation split)."""

    documents: int
    field_exact_rate: float
    field_normalized_rate: float
    line_cell_exact_rate: float


@dataclass(frozen=True)
class EvaluationReport:
    documents_scored: int
    documents_failed: int
    field_exact_rate: float
    field_normalized_rate: float
    line_cell_exact_rate: float
    class_accuracy: float | None
    #: Documents the candidate would auto-approve despite at least one
    #: wrong field, over all scored documents — the gate's key signal.
    false_auto_approval_rate: float
    mean_latency_ms: float
    total_cost_cents: int
    by_field: dict[str, FieldScore]
    by_split: dict[str, int]
    by_cohort: dict[str, CohortMetrics]
    errors: dict[str, str]

    def to_json(self) -> str:
        payload = {
            **{k: v for k, v in vars(self).items() if k not in ("by_field", "by_cohort")},
            "by_field": {key: vars(score) for key, score in self.by_field.items()},
            "by_cohort": {key: vars(metrics) for key, metrics in self.by_cohort.items()},
        }
        return json.dumps(payload, sort_keys=True)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def build_report(
    state: EvaluationState,
    documents: list[EvalDocument],
    by_field: dict[str, FieldScore] | None = None,
) -> EvaluationReport:
    """Aggregate everything the state has scored. ``by_field`` is the
    per-field breakdown collected at scoring time (it is not part of
    the resumable checkpoint, so it covers the current run)."""
    scores = [
        state.scores[doc.document_sha256]
        for doc in documents
        if doc.document_sha256 in state.scores
    ]
    fields_total = sum(score.fields_total for score in scores)
    class_scores = [score.class_correct for score in scores if score.class_correct is not None]
    false_approvals = sum(1 for score in scores if score.auto_approved and score.wrong_fields)

    def cohort_metrics(cohort_scores: list[DocumentScore]) -> CohortMetrics:
        totals = sum(s.fields_total for s in cohort_scores)
        return CohortMetrics(
            documents=len(cohort_scores),
            field_exact_rate=_rate(sum(s.fields_exact for s in cohort_scores), totals),
            field_normalized_rate=_rate(sum(s.fields_normalized for s in cohort_scores), totals),
            line_cell_exact_rate=_rate(
                sum(s.line_cells_exact for s in cohort_scores),
                sum(s.line_cells_total for s in cohort_scores),
            ),
        )

    by_cohort = {
        split: cohort_metrics([s for s in scores if s.split == split])
        for split in sorted({s.split for s in scores})
    }
    return EvaluationReport(
        documents_scored=len(scores),
        documents_failed=len(state.errors),
        field_exact_rate=_rate(sum(s.fields_exact for s in scores), fields_total),
        field_normalized_rate=_rate(sum(s.fields_normalized for s in scores), fields_total),
        line_cell_exact_rate=_rate(
            sum(s.line_cells_exact for s in scores),
            sum(s.line_cells_total for s in scores),
        ),
        class_accuracy=(
            _rate(sum(1 for c in class_scores if c), len(class_scores)) if class_scores else None
        ),
        false_auto_approval_rate=_rate(false_approvals, len(scores)),
        mean_latency_ms=(
            round(sum(s.latency_ms for s in scores) / len(scores), 2) if scores else 0.0
        ),
        total_cost_cents=sum(s.cost_cents for s in scores),
        by_field=dict(by_field if by_field is not None else state.by_field),
        by_split={split: metrics.documents for split, metrics in by_cohort.items()},
        by_cohort=by_cohort,
        errors=dict(state.errors),
    )


async def run_evaluation(
    documents: list[EvalDocument],
    extract_fn: ExtractFn,
    *,
    provider_info: ProviderInfo | None = None,
    allow_external: bool = False,
    state: EvaluationState | None = None,
    clock: Clock | None = None,
) -> tuple[EvaluationReport, EvaluationState]:
    """Run the candidate over every not-yet-scored document. Returns the
    aggregated report and the checkpoint state."""
    if (
        provider_info is not None
        and provider_info.data_policy.sends_content_to_third_party
        and not allow_external
    ):
        raise EvaluationRefusedError(
            f"provider {provider_info.name!r} sends content to a third party; "
            "evaluations make no external calls unless allow_external=True is "
            "passed explicitly"
        )

    effective_state = state or EvaluationState()
    field_breakdown = effective_state.by_field
    ordered = sorted(documents, key=lambda doc: doc.document_sha256)
    for document in ordered:
        sha = document.document_sha256
        if sha in effective_state.scores or sha in effective_state.errors:
            continue  # resumable: never pay twice
        started = clock() if clock else 0.0
        try:
            prediction = await extract_fn(document)
        except Exception as error:
            effective_state.errors[sha] = str(error) or error.__class__.__name__
            continue
        elapsed_ms = ((clock() - started) * 1000.0) if clock else 0.0
        score, contribution = score_document(document, prediction, latency_ms=elapsed_ms)
        effective_state.scores[sha] = score
        for key, part in contribution.items():
            existing = field_breakdown.get(key, FieldScore())
            field_breakdown[key] = FieldScore(
                total=existing.total + part.total,
                exact=existing.exact + part.exact,
                normalized=existing.normalized + part.normalized,
            )

    effective_state.by_field = field_breakdown

    return build_report(effective_state, ordered, field_breakdown), effective_state


__all__ = [
    "CohortMetrics",
    "DocumentScore",
    "EvalDocument",
    "EvalPrediction",
    "EvaluationRefusedError",
    "EvaluationReport",
    "EvaluationState",
    "FieldScore",
    "build_report",
    "run_evaluation",
    "score_document",
]
