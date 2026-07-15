"""Baseline confidence policy (PRC-011).

Decides where a document goes after validation: straight through
("approved") or to a human ("review_required") — and always says WHY.

Provider confidence is exactly one signal among several, never the
decision. The policy weighs, per field: presence, the provider's
confidence gated by the field's schema criticality (CFG-003), whether
the value carries evidence, and whether a competing candidate reading
was close enough to make the chosen one ambiguous. On top of that sit
the rule results (PRC-009): triggered blocking/review rules route to
review, and an INDETERMINATE error-severity rule routes to review too —
"we could not check it" is not "it passed".

Critical fields get conservative gates: a wrong critical value is a
wrong order, so they need high confidence, evidence, and a clear margin
over rival readings. Every gate is an explicit, versioned number below;
changing one is a new POLICY_VERSION.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from soa_rules.evaluator import RuleSetEvaluation

POLICY_VERSION = "1.0.0"

CRITICALITIES = ("critical", "standard", "informational")


@dataclass(frozen=True)
class ConfidencePolicy:
    """The versioned gates. Defaults are the platform baseline."""

    version: str = POLICY_VERSION
    #: Minimum provider confidence per criticality. Informational fields
    #: never gate routing — they are captured, not enforced.
    critical_min_confidence: float = 0.98
    standard_min_confidence: float = 0.85
    #: Optional exact field gates from a pinned tenant confidence policy.
    field_min_confidence: Mapping[str, float] = field(default_factory=dict)
    #: Critical values must carry evidence to auto-approve.
    critical_requires_evidence: bool = True
    #: A rival reading within this margin of the chosen one makes the
    #: field ambiguous (chosen - margin <= rival).
    critical_candidate_margin: float = 0.20
    standard_candidate_margin: float = 0.05
    #: Indeterminate error-severity rules route to review.
    review_on_indeterminate_error_rules: bool = True


@dataclass(frozen=True)
class FieldSignal:
    """The policy-relevant facts about one extracted field (adapter from
    PRC-007 rows arrives with the pipeline stage, PRC-012)."""

    field_key: str
    criticality: str  # critical | standard | informational
    present: bool
    confidence: float
    has_evidence: bool
    row_index: int | None = None
    #: Confidence of the strongest rival reading, if any.
    top_candidate_confidence: float | None = None


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    field_key: str | None = None
    row_index: int | None = None
    rule_key: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field_key": self.field_key,
            "row_index": self.row_index,
            "rule_key": self.rule_key,
        }


@dataclass(frozen=True)
class RouteDecision:
    route: str  # approved | review_required
    reasons: tuple[Reason, ...]
    policy_version: str = POLICY_VERSION

    @property
    def approved(self) -> bool:
        return self.route == "approved"

    def to_json(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "policy_version": self.policy_version,
            "reasons": [reason.to_json() for reason in self.reasons],
        }


def _min_confidence(policy: ConfidencePolicy, criticality: str, field_key: str) -> float | None:
    override = policy.field_min_confidence.get(field_key)
    if override is not None:
        return override
    if criticality == "critical":
        return policy.critical_min_confidence
    if criticality == "standard":
        return policy.standard_min_confidence
    return None  # informational: no gate


def _candidate_margin(policy: ConfidencePolicy, criticality: str) -> float | None:
    if criticality == "critical":
        return policy.critical_candidate_margin
    if criticality == "standard":
        return policy.standard_candidate_margin
    return None


def _field_reasons(signal: FieldSignal, policy: ConfidencePolicy) -> list[Reason]:
    if signal.criticality not in CRITICALITIES:
        raise ValueError(f"unknown criticality {signal.criticality!r}")
    if not signal.present:
        if signal.criticality == "critical":
            return [
                Reason(
                    code="missing_critical_field",
                    message=f"critical field {signal.field_key!r} was not extracted",
                    field_key=signal.field_key,
                    row_index=signal.row_index,
                )
            ]
        return []  # non-critical absence is the rules' business, not a gate

    reasons: list[Reason] = []
    gate = _min_confidence(policy, signal.criticality, signal.field_key)
    if gate is not None and signal.confidence < gate:
        reasons.append(
            Reason(
                code="low_confidence",
                message=(
                    f"confidence {signal.confidence:.2f} is below the "
                    f"{signal.criticality} gate of {gate:.2f}"
                ),
                field_key=signal.field_key,
                row_index=signal.row_index,
            )
        )
    if (
        signal.criticality == "critical"
        and policy.critical_requires_evidence
        and not signal.has_evidence
    ):
        reasons.append(
            Reason(
                code="missing_evidence",
                message=f"critical field {signal.field_key!r} has no source evidence",
                field_key=signal.field_key,
                row_index=signal.row_index,
            )
        )
    margin = _candidate_margin(policy, signal.criticality)
    if (
        margin is not None
        and signal.top_candidate_confidence is not None
        and signal.top_candidate_confidence >= signal.confidence - margin
    ):
        reasons.append(
            Reason(
                code="ambiguous_reading",
                message=(
                    f"a rival reading at {signal.top_candidate_confidence:.2f} is within "
                    f"{margin:.2f} of the chosen reading at {signal.confidence:.2f}"
                ),
                field_key=signal.field_key,
                row_index=signal.row_index,
            )
        )
    return reasons


def decide_route(
    signals: list[FieldSignal] | tuple[FieldSignal, ...],
    evaluation: RuleSetEvaluation,
    policy: ConfidencePolicy | None = None,
) -> RouteDecision:
    """Deterministic route decision with every reason enumerated. The
    document is approved ONLY when nothing at all asked for a human."""
    effective = policy or ConfidencePolicy()
    reasons: list[Reason] = []

    for finding in evaluation.triggered():
        if finding.action in ("block", "route_to_review"):
            reasons.append(
                Reason(
                    code="rule_triggered",
                    message=finding.message or f"rule {finding.rule_key!r} triggered",
                    rule_key=finding.rule_key,
                    row_index=finding.row_index,
                )
            )
    if effective.review_on_indeterminate_error_rules:
        for finding in evaluation.indeterminate():
            if finding.severity == "error":
                reasons.append(
                    Reason(
                        code="rule_indeterminate",
                        message=(
                            f"rule {finding.rule_key!r} could not be checked — "
                            "unverified is not verified"
                        ),
                        rule_key=finding.rule_key,
                        row_index=finding.row_index,
                    )
                )

    for signal in signals:
        reasons.extend(_field_reasons(signal, effective))

    route = "approved" if not reasons else "review_required"
    return RouteDecision(route=route, reasons=tuple(reasons), policy_version=effective.version)
