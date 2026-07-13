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
]
