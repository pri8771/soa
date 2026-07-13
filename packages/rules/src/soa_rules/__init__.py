"""Deterministic rule evaluation (PRC-009).

Executes the CFG-004 typed expression AST — plus runtime-only arithmetic,
aggregation, and tolerance ops — over a document's canonical field values
(PRC-008 output). Pure, deterministic, bounded, and fully traced: every
finding carries an annotated copy of its expression tree showing each
operand's resolved value, so "why did this rule fire" is answerable from
the trace alone.
"""

from soa_rules.confidence import (
    ConfidencePolicy,
    FieldSignal,
    Reason,
    RouteDecision,
    decide_route,
)
from soa_rules.evaluator import (
    EvaluationInput,
    EvaluationLimits,
    RuleComplexityError,
    RuleDefinitionError,
    RuleFinding,
    RuleSetEvaluation,
    evaluate_expression,
    evaluate_rule_set,
)

__all__ = [
    "ConfidencePolicy",
    "EvaluationInput",
    "EvaluationLimits",
    "FieldSignal",
    "Reason",
    "RouteDecision",
    "RuleComplexityError",
    "RuleDefinitionError",
    "RuleFinding",
    "RuleSetEvaluation",
    "decide_route",
    "evaluate_expression",
    "evaluate_rule_set",
]
