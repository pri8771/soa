"""Confidence policy tests (PRC-011): the decision matrix — every gate,
every reason code, and the proof that provider confidence alone never
approves a document."""

import pytest

from soa_rules import (
    ConfidencePolicy,
    EvaluationInput,
    FieldSignal,
    RouteDecision,
    decide_route,
    evaluate_rule_set,
)
from soa_rules.evaluator import RuleSetEvaluation

CLEAN_EVALUATION = RuleSetEvaluation(findings=(), derived=())


def signal(**overrides: object) -> FieldSignal:
    base: dict[str, object] = {
        "field_key": "po_number",
        "criticality": "critical",
        "present": True,
        "confidence": 0.99,
        "has_evidence": True,
    }
    base.update(overrides)
    return FieldSignal(**base)  # type: ignore[arg-type]


def codes(decision: RouteDecision) -> list[str]:
    return [reason.code for reason in decision.reasons]


def test_everything_clean_approves_with_no_reasons() -> None:
    decision = decide_route([signal()], CLEAN_EVALUATION)
    assert decision.approved
    assert decision.reasons == ()
    assert decision.policy_version == "1.0.0"


@pytest.mark.parametrize(
    ("criticality", "confidence", "expected_codes"),
    [
        ("critical", 0.99, []),
        ("critical", 0.97, ["low_confidence"]),  # below the 0.98 gate
        ("standard", 0.86, []),
        ("standard", 0.84, ["low_confidence"]),  # below the 0.85 gate
        ("informational", 0.10, []),  # informational never gates
    ],
)
def test_confidence_gates_by_criticality(
    criticality: str, confidence: float, expected_codes: list[str]
) -> None:
    decision = decide_route(
        [signal(criticality=criticality, confidence=confidence)], CLEAN_EVALUATION
    )
    assert codes(decision) == expected_codes


def test_critical_fields_require_evidence() -> None:
    decision = decide_route([signal(has_evidence=False)], CLEAN_EVALUATION)
    assert codes(decision) == ["missing_evidence"]
    # Standard fields do not carry the evidence gate.
    decision = decide_route(
        [signal(criticality="standard", confidence=0.9, has_evidence=False)],
        CLEAN_EVALUATION,
    )
    assert decision.approved


def test_missing_critical_field_routes_to_review() -> None:
    decision = decide_route(
        [signal(present=False, confidence=0.0, has_evidence=False)], CLEAN_EVALUATION
    )
    assert codes(decision) == ["missing_critical_field"]
    (reason,) = decision.reasons
    assert reason.field_key == "po_number"
    # A missing informational field is not a routing matter.
    decision = decide_route(
        [signal(criticality="informational", present=False, confidence=0.0, has_evidence=False)],
        CLEAN_EVALUATION,
    )
    assert decision.approved


@pytest.mark.parametrize(
    ("criticality", "chosen", "rival", "ambiguous"),
    [
        ("critical", 0.99, 0.80, True),  # within the 0.20 critical margin
        ("critical", 0.99, 0.78, False),  # clear of it
        ("standard", 0.90, 0.86, True),  # within the 0.05 standard margin
        ("standard", 0.90, 0.84, False),
        ("informational", 0.90, 0.89, False),  # informational never gates
    ],
)
def test_rival_readings_within_margin_are_ambiguous(
    criticality: str, chosen: float, rival: float, ambiguous: bool
) -> None:
    decision = decide_route(
        [
            signal(
                criticality=criticality,
                confidence=chosen,
                top_candidate_confidence=rival,
            )
        ],
        CLEAN_EVALUATION,
    )
    assert ("ambiguous_reading" in codes(decision)) is ambiguous


def test_provider_confidence_is_only_one_signal() -> None:
    # Perfect confidence everywhere — but a blocking rule triggered.
    triggered = evaluate_rule_set(
        [
            {
                "key": "totals.mismatch",
                "severity": "error",
                "action": "block",
                "condition": {"op": "const", "value": True},
                "message": "totals disagree",
            }
        ],
        EvaluationInput(header={}),
    )
    decision = decide_route([signal(confidence=1.0)], triggered)
    assert not decision.approved
    (reason,) = decision.reasons
    assert reason.code == "rule_triggered"
    assert reason.rule_key == "totals.mismatch"
    assert reason.message == "totals disagree"


def test_indeterminate_error_rules_route_to_review() -> None:
    # A reconciliation that could not run is not a pass.
    indeterminate = evaluate_rule_set(
        [
            {
                "key": "totals.check",
                "severity": "error",
                "action": "block",
                "condition": {
                    "op": "gt",
                    "left": {"op": "field", "key": "missing"},
                    "right": {"op": "const", "value": "1"},
                },
            }
        ],
        EvaluationInput(header={}),
    )
    decision = decide_route([signal()], indeterminate)
    assert codes(decision) == ["rule_indeterminate"]
    # The gate is policy, and the policy is explicit.
    relaxed = ConfidencePolicy(review_on_indeterminate_error_rules=False)
    assert decide_route([signal()], indeterminate, relaxed).approved


def test_annotate_rules_do_not_route() -> None:
    annotated = evaluate_rule_set(
        [
            {
                "key": "fyi",
                "severity": "info",
                "action": "annotate",
                "condition": {"op": "const", "value": True},
            }
        ],
        EvaluationInput(header={}),
    )
    assert decide_route([signal()], annotated).approved


def test_reasons_accumulate_and_identify_their_sources() -> None:
    decision = decide_route(
        [
            signal(confidence=0.90),  # low for critical
            signal(field_key="lines.sku", criticality="standard", confidence=0.5, row_index=1),
        ],
        CLEAN_EVALUATION,
    )
    assert decision.route == "review_required"
    assert codes(decision) == ["low_confidence", "low_confidence"]
    assert decision.reasons[1].field_key == "lines.sku"
    assert decision.reasons[1].row_index == 1
    payload = decision.to_json()
    assert payload["route"] == "review_required"
    assert len(payload["reasons"]) == 2


def test_unknown_criticality_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown criticality"):
        decide_route([signal(criticality="serious")], CLEAN_EVALUATION)


def test_custom_policy_gates_apply() -> None:
    strict = ConfidencePolicy(version="strict-test", standard_min_confidence=0.95)
    decision = decide_route(
        [signal(criticality="standard", confidence=0.9)], CLEAN_EVALUATION, strict
    )
    assert codes(decision) == ["low_confidence"]
    assert decision.policy_version == "strict-test"
