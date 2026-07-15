"""Evaluation comparison and promotion gate (AIO-017).

Compares the CURRENT configuration's evaluation report against a
CANDIDATE's (both from the AIO-016 runner over the same published gold
dataset version) and decides whether the candidate may be promoted.

The comparison is per field and per cohort — aggregate averages never
hide a critical regression, because every finding names the specific
field or cohort that got worse.

The gate:

- a per-field EXACT-rate drop beyond the policy tolerance is a
  REGRESSION finding (waivable with authority + reason);
- ANY drop on a field the policy marks critical, and ANY increase of
  the false-auto-approval rate, are PROHIBITED findings — publication
  is blocked and NO waiver can bypass them (an authorized human can
  accept "slightly worse on a cosmetic field", nobody is authorized to
  accept silently auto-approving wrong critical data);
- ``assert_promotable`` enforces the verdict: candidates with findings
  do not publish without a waiver; a waiver requires an actor and a
  written reason and is handed to the audit sink BEFORE promotion
  proceeds; prohibited findings block regardless.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from soa_worker.evaluation import EvaluationReport


@dataclass(frozen=True)
class GatePolicy:
    #: Tolerated per-field exact-rate drop before it counts as a regression.
    max_field_regression: float = 0.02
    #: Fields where ANY regression is prohibited (never waivable).
    critical_fields: tuple[str, ...] = ()
    #: Tolerated per-cohort exact-rate drop.
    max_cohort_regression: float = 0.02
    #: Absolute floors apply to every promotion, including the first
    #: candidate where no baseline exists.  Relative improvement is never a
    #: substitute for an acceptable result.
    min_documents_scored: int = 1
    max_documents_failed: int = 0
    min_field_exact_rate: float = 0.95
    min_field_normalized_rate: float = 0.98
    min_line_cell_exact_rate: float = 0.95
    min_class_accuracy: float = 0.95
    max_false_auto_approval_rate: float = 0.0
    min_critical_field_exact_rate: float = 1.0


@dataclass(frozen=True)
class FieldDiff:
    field: str
    current_exact_rate: float | None
    candidate_exact_rate: float | None
    delta: float | None


@dataclass(frozen=True)
class CohortDiff:
    cohort: str
    current_exact_rate: float | None
    candidate_exact_rate: float | None
    delta: float | None


@dataclass(frozen=True)
class Finding:
    #: field_regression | critical_field_regression | cohort_regression
    #: | false_auto_approval
    kind: str
    detail: str
    #: Prohibited findings can never be waived.
    waivable: bool


@dataclass(frozen=True)
class GateResult:
    field_diffs: tuple[FieldDiff, ...]
    cohort_diffs: tuple[CohortDiff, ...]
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def prohibited(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if not finding.waivable)


@dataclass(frozen=True)
class Waiver:
    actor_id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("a waiver needs the authorizing actor")
        if not self.reason.strip():
            raise ValueError("a waiver needs a written reason — 'because' is not governance")


class PromotionBlockedError(Exception):
    def __init__(self, findings: tuple[Finding, ...], message: str) -> None:
        self.findings = findings
        super().__init__(message)


def _field_rate(report: EvaluationReport, field: str) -> float | None:
    score = report.by_field.get(field)
    if score is None or score.total == 0:
        return None
    return round(score.exact / score.total, 4)


def compare_reports(
    current: EvaluationReport,
    candidate: EvaluationReport,
    policy: GatePolicy | None = None,
) -> GateResult:
    """Diff the two reports and produce the gate findings."""
    effective = policy or GatePolicy()
    findings: list[Finding] = []

    field_diffs: list[FieldDiff] = []
    for field in sorted(set(current.by_field) | set(candidate.by_field)):
        current_rate = _field_rate(current, field)
        candidate_rate = _field_rate(candidate, field)
        delta = (
            round(candidate_rate - current_rate, 4)
            if current_rate is not None and candidate_rate is not None
            else None
        )
        field_diffs.append(
            FieldDiff(
                field=field,
                current_exact_rate=current_rate,
                candidate_exact_rate=candidate_rate,
                delta=delta,
            )
        )
        if delta is None:
            continue
        if field in effective.critical_fields and delta < 0:
            findings.append(
                Finding(
                    kind="critical_field_regression",
                    detail=(
                        f"critical field {field!r} regressed from {current_rate:.2%} "
                        f"to {candidate_rate:.2%} — prohibited, no waiver applies"
                    ),
                    waivable=False,
                )
            )
        elif delta < -effective.max_field_regression:
            findings.append(
                Finding(
                    kind="field_regression",
                    detail=(
                        f"field {field!r} regressed from {current_rate:.2%} to "
                        f"{candidate_rate:.2%} (tolerance {effective.max_field_regression:.2%})"
                    ),
                    waivable=True,
                )
            )

    cohort_diffs: list[CohortDiff] = []
    for cohort in sorted(set(current.by_cohort) | set(candidate.by_cohort)):
        current_metrics = current.by_cohort.get(cohort)
        candidate_metrics = candidate.by_cohort.get(cohort)
        current_rate = current_metrics.field_exact_rate if current_metrics else None
        candidate_rate = candidate_metrics.field_exact_rate if candidate_metrics else None
        delta = (
            round(candidate_rate - current_rate, 4)
            if current_rate is not None and candidate_rate is not None
            else None
        )
        cohort_diffs.append(
            CohortDiff(
                cohort=cohort,
                current_exact_rate=current_rate,
                candidate_exact_rate=candidate_rate,
                delta=delta,
            )
        )
        if delta is not None and delta < -effective.max_cohort_regression:
            findings.append(
                Finding(
                    kind="cohort_regression",
                    detail=(
                        f"cohort {cohort!r} regressed from {current_rate:.2%} to "
                        f"{candidate_rate:.2%} (tolerance {effective.max_cohort_regression:.2%})"
                    ),
                    waivable=True,
                )
            )

    if candidate.false_auto_approval_rate > current.false_auto_approval_rate:
        findings.append(
            Finding(
                kind="false_auto_approval",
                detail=(
                    f"the false-auto-approval rate rose from "
                    f"{current.false_auto_approval_rate:.2%} to "
                    f"{candidate.false_auto_approval_rate:.2%} — the candidate would "
                    "auto-approve more documents containing wrong values; prohibited"
                ),
                waivable=False,
            )
        )

    return GateResult(
        field_diffs=tuple(field_diffs),
        cohort_diffs=tuple(cohort_diffs),
        findings=tuple(findings),
    )


def _absolute_quality_findings(
    candidate: EvaluationReport, policy: GatePolicy
) -> tuple[Finding, ...]:
    """Return non-waivable findings for absolute quality floors.

    These checks are deliberately independent of a baseline.  They prevent
    both a low-quality first publication and a candidate that is "better"
    than an even worse baseline from becoming promotion evidence.
    """
    findings: list[Finding] = []

    def prohibited(kind: str, detail: str) -> None:
        findings.append(Finding(kind=kind, detail=detail, waivable=False))

    if candidate.documents_scored < policy.min_documents_scored:
        prohibited(
            "insufficient_documents",
            f"only {candidate.documents_scored} documents were scored; "
            f"at least {policy.min_documents_scored} are required",
        )
    if candidate.documents_failed > policy.max_documents_failed:
        prohibited(
            "evaluation_errors",
            f"{candidate.documents_failed} documents failed evaluation; "
            f"at most {policy.max_documents_failed} are allowed",
        )
    if candidate.field_exact_rate < policy.min_field_exact_rate:
        prohibited(
            "field_exact_below_minimum",
            f"field exact accuracy {candidate.field_exact_rate:.2%} is below the "
            f"{policy.min_field_exact_rate:.2%} minimum",
        )
    if candidate.field_normalized_rate < policy.min_field_normalized_rate:
        prohibited(
            "field_normalized_below_minimum",
            f"normalized field accuracy {candidate.field_normalized_rate:.2%} is below the "
            f"{policy.min_field_normalized_rate:.2%} minimum",
        )

    line_fields = {
        key: score for key, score in candidate.by_field.items() if key.startswith("lines.")
    }
    if line_fields and candidate.line_cell_exact_rate < policy.min_line_cell_exact_rate:
        prohibited(
            "line_cell_exact_below_minimum",
            f"line-cell exact accuracy {candidate.line_cell_exact_rate:.2%} is below the "
            f"{policy.min_line_cell_exact_rate:.2%} minimum",
        )
    if (
        candidate.class_accuracy is not None
        and candidate.class_accuracy < policy.min_class_accuracy
    ):
        prohibited(
            "class_accuracy_below_minimum",
            f"classification accuracy {candidate.class_accuracy:.2%} is below the "
            f"{policy.min_class_accuracy:.2%} minimum",
        )
    if candidate.false_auto_approval_rate > policy.max_false_auto_approval_rate:
        prohibited(
            "false_auto_approval_above_maximum",
            f"false-auto-approval rate {candidate.false_auto_approval_rate:.2%} exceeds the "
            f"{policy.max_false_auto_approval_rate:.2%} maximum",
        )

    for field in policy.critical_fields:
        rate = _field_rate(candidate, field)
        if rate is None:
            prohibited(
                "critical_field_unmeasured",
                f"critical field {field!r} is not measured by this evaluation dataset",
            )
        elif rate < policy.min_critical_field_exact_rate:
            prohibited(
                "critical_field_below_minimum",
                f"critical field {field!r} exact accuracy {rate:.2%} is below the "
                f"{policy.min_critical_field_exact_rate:.2%} minimum",
            )
    return tuple(findings)


def evaluate_promotion_gate(
    candidate: EvaluationReport,
    *,
    current: EvaluationReport | None = None,
    policy: GatePolicy | None = None,
) -> GateResult:
    """Apply absolute thresholds and, when supplied, baseline regressions."""
    effective = policy or GatePolicy()
    regression = (
        compare_reports(current, candidate, effective)
        if current is not None
        else GateResult(field_diffs=(), cohort_diffs=(), findings=())
    )
    return GateResult(
        field_diffs=regression.field_diffs,
        cohort_diffs=regression.cohort_diffs,
        findings=(*_absolute_quality_findings(candidate, effective), *regression.findings),
    )


AuditSink = Callable[[dict[str, Any]], None]


def assert_promotable(
    result: GateResult,
    *,
    waiver: Waiver | None = None,
    audit_sink: AuditSink | None = None,
) -> None:
    """Raise :class:`PromotionBlockedError` unless the candidate may
    publish. A clean result passes. Findings need a waiver (actor +
    reason, recorded via ``audit_sink``); prohibited findings block
    regardless of any waiver."""
    if result.passed:
        return
    prohibited = result.prohibited
    if prohibited:
        raise PromotionBlockedError(
            prohibited,
            "publication blocked: "
            + "; ".join(finding.detail for finding in prohibited)
            + " — prohibited findings cannot be waived",
        )
    if waiver is None:
        raise PromotionBlockedError(
            result.findings,
            "publication blocked: "
            + "; ".join(finding.detail for finding in result.findings)
            + " — an authorized waiver with a written reason is required",
        )
    if audit_sink is not None:
        audit_sink(
            {
                "action": "evaluation.gate_waived",
                "actor_id": waiver.actor_id,
                "reason": waiver.reason,
                "findings": [finding.detail for finding in result.findings],
            }
        )


__all__ = [
    "AuditSink",
    "CohortDiff",
    "FieldDiff",
    "Finding",
    "GatePolicy",
    "GateResult",
    "PromotionBlockedError",
    "Waiver",
    "assert_promotable",
    "compare_reports",
    "evaluate_promotion_gate",
]
