"""Rule evaluator tests (PRC-009): evaluation matrix, three-valued
honesty, traces, row scope, derived values, and complexity limits."""

from decimal import Decimal
from typing import Any

import pytest

from soa_rules import (
    EvaluationInput,
    EvaluationLimits,
    RuleComplexityError,
    RuleDefinitionError,
    evaluate_expression,
    evaluate_rule_set,
)

HEADER = {
    "po_number": "PO-100042",
    "order_date": "2026-03-14",
    "delivery_date": "2026-04-01",
    "currency": "USD",
    "total_amount": {"amount": "1234.50", "currency": "USD"},
    "discount": None,
}
LINES = [
    {
        "lines.sku": "WID-100",
        "lines.quantity": "10",
        "lines.unit_price": "45.00",
        "lines.line_total": "450.00",
    },
    {
        "lines.sku": "GAD-205",
        "lines.quantity": "3",
        "lines.unit_price": "261.50",
        "lines.line_total": "784.50",
    },
]
DATA = EvaluationInput(header=HEADER, tables={"lines": LINES})


def fld(key: str) -> dict[str, Any]:
    return {"op": "field", "key": key}


def const(value: Any) -> dict[str, Any]:
    return {"op": "const", "value": value}


def rule(key: str, condition: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"key": key, "severity": "error", "action": "block", "condition": condition, **extra}


# -- expression matrix -----------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ({"op": "eq", "left": fld("po_number"), "right": const("PO-100042")}, True),
        ({"op": "ne", "left": fld("po_number"), "right": const("PO-1")}, True),
        # ISO dates order lexically — exactly why the canonical form is ISO.
        ({"op": "lt", "left": fld("order_date"), "right": fld("delivery_date")}, True),
        # Canonical decimal strings compare numerically.
        ({"op": "gt", "left": fld("total_amount"), "right": const("1000")}, True),
        ({"op": "gte", "left": const("2"), "right": const("10")}, False),
        # Money equality includes the currency.
        (
            {
                "op": "eq",
                "left": fld("total_amount"),
                "right": const({"amount": "1234.50", "currency": "USD"}),
            },
            True,
        ),
        (
            {
                "op": "eq",
                "left": fld("total_amount"),
                "right": const({"amount": "1234.50", "currency": "EUR"}),
            },
            False,
        ),
        ({"op": "is_present", "key": "po_number"}, True),
        ({"op": "is_present", "key": "discount"}, False),
        ({"op": "not", "arg": {"op": "is_present", "key": "discount"}}, True),
        (
            {
                "op": "and",
                "args": [
                    {"op": "is_present", "key": "po_number"},
                    {"op": "is_present", "key": "currency"},
                ],
            },
            True,
        ),
        # Arithmetic is exact decimal.
        (
            {
                "op": "eq",
                "left": {"op": "mul", "left": const("0.1"), "right": const("3")},
                "right": const("0.3"),
            },
            True,
        ),
        (
            {
                "op": "eq",
                "left": {"op": "add", "left": const("450.00"), "right": const("784.50")},
                "right": const("1234.50"),
            },
            True,
        ),
        # Aggregation over rows.
        (
            {
                "op": "eq",
                "left": {"op": "sum", "key": "lines.line_total"},
                "right": fld("total_amount"),
            },
            True,
        ),
        ({"op": "eq", "left": {"op": "row_count", "key": "lines"}, "right": const(2)}, True),
        # Explicit tolerances.
        (
            {
                "op": "approx_eq",
                "left": const("100.00"),
                "right": const("100.40"),
                "tolerance": const("0.50"),
            },
            True,
        ),
        (
            {
                "op": "approx_eq",
                "left": const("100.00"),
                "right": const("101.00"),
                "relative_tolerance": const("0.02"),
            },
            True,
        ),
        # No hidden slack: omitted tolerance means exactly zero.
        ({"op": "approx_eq", "left": const("100.00"), "right": const("100.01")}, False),
    ],
)
def test_expression_matrix(condition: dict[str, Any], expected: bool) -> None:
    value, _trace = evaluate_expression(condition, DATA)
    assert value is expected


@pytest.mark.parametrize(
    "condition",
    [
        {"op": "gt", "left": fld("discount"), "right": const("5")},  # missing operand
        {"op": "gt", "left": fld("po_number"), "right": const("5")},  # text vs number
        {"op": "div", "left": const("10"), "right": const("0")},  # division by zero
        {  # ordering money across currencies
            "op": "gt",
            "left": const({"amount": "5", "currency": "USD"}),
            "right": const({"amount": "4", "currency": "EUR"}),
        },
    ],
)
def test_indeterminate_is_none_not_false(condition: dict[str, Any]) -> None:
    value, trace = evaluate_expression(condition, DATA)
    assert value is None
    assert trace["indeterminate"] is True


def test_kleene_logic_short_circuits_honestly() -> None:
    missing = {"op": "gt", "left": fld("discount"), "right": const("5")}
    false = {"op": "eq", "left": const("1"), "right": const("2")}
    true = {"op": "eq", "left": const("1"), "right": const("1")}
    # AND with a false arg is false even when another arg is indeterminate.
    value, _ = evaluate_expression({"op": "and", "args": [missing, false]}, DATA)
    assert value is False
    value, _ = evaluate_expression({"op": "and", "args": [missing, true]}, DATA)
    assert value is None
    value, _ = evaluate_expression({"op": "or", "args": [missing, true]}, DATA)
    assert value is True
    value, _ = evaluate_expression({"op": "or", "args": [missing, false]}, DATA)
    assert value is None
    value, _ = evaluate_expression({"op": "not", "arg": missing}, DATA)
    assert value is None


def test_trace_explains_operands_and_result() -> None:
    condition = {"op": "gt", "left": fld("total_amount"), "right": const("1000")}
    value, trace = evaluate_expression(condition, DATA)
    assert value is True
    assert trace["left"] == {
        "op": "field",
        "key": "total_amount",
        "value": {"amount": "1234.50", "currency": "USD"},
    }
    assert trace["right"] == {"op": "const", "value": "1000"}
    assert trace["result"] is True


def test_sum_over_missing_cells_is_indeterminate_with_named_rows() -> None:
    rows = [dict(LINES[0]), {"lines.sku": "X"}]  # second row has no line_total
    data = EvaluationInput(header=HEADER, tables={"lines": rows})
    value, trace = evaluate_expression({"op": "sum", "key": "lines.line_total"}, data)
    assert value is None
    assert trace["missing_rows"] == [1]
    assert trace["indeterminate"] is True


# -- rule-set evaluation -----------------------------------------------------------


def test_header_and_row_rules_with_status_and_row_identity() -> None:
    rules = [
        rule(
            "po_required",
            {"op": "not", "arg": {"op": "is_present", "key": "po_number"}},
            message="PO number is required",
        ),
        rule(
            "line_total_matches",
            {
                "op": "not",
                "arg": {
                    "op": "approx_eq",
                    "left": {
                        "op": "mul",
                        "left": fld("lines.quantity"),
                        "right": fld("lines.unit_price"),
                    },
                    "right": fld("lines.line_total"),
                    "tolerance": const("0.01"),
                },
            },
            scope={"table": "lines"},
        ),
        rule(
            "discount_check",
            {"op": "gt", "left": fld("discount"), "right": const("50")},
            severity="warning",
            action="route_to_review",
        ),
    ]
    result = evaluate_rule_set(rules, DATA)
    by_key = {(f.rule_key, f.row_index): f for f in result.findings}
    assert by_key[("po_required", None)].status == "passed"
    assert by_key[("line_total_matches", 0)].status == "passed"
    assert by_key[("line_total_matches", 1)].status == "passed"
    assert by_key[("discount_check", None)].status == "indeterminate"
    assert result.blocking is False
    assert result.review_required is False
    summary = result.summary()
    assert summary["rules_evaluated"] == 4  # 2 header + 2 rows
    assert summary["indeterminate"] == 1


def test_triggered_block_rule_blocks_and_requires_review() -> None:
    bad_lines = [dict(LINES[0], **{"lines.line_total": "999.99"})]
    data = EvaluationInput(header=HEADER, tables={"lines": bad_lines})
    rules = [
        rule(
            "line_total_matches",
            {
                "op": "ne",
                "left": {
                    "op": "mul",
                    "left": fld("lines.quantity"),
                    "right": fld("lines.unit_price"),
                },
                "right": fld("lines.line_total"),
            },
            scope={"table": "lines"},
        )
    ]
    result = evaluate_rule_set(rules, data)
    (finding,) = result.findings
    assert finding.status == "triggered"
    assert finding.row_index == 0
    assert result.blocking is True
    assert result.review_required is True
    # The trace shows the actual operands that disagreed.
    assert finding.trace["left"]["result"] == "450.00"
    assert finding.trace["right"]["value"] == "999.99"


def test_derived_values_fill_gaps_but_never_overwrite() -> None:
    rows = [
        {"lines.quantity": "10", "lines.unit_price": "45.00"},  # no line_total
        {
            "lines.quantity": "3",
            "lines.unit_price": "261.50",
            "lines.line_total": "784.50",
        },  # present: untouched
        {"lines.quantity": "2"},  # unit_price missing: nothing derivable
    ]
    data = EvaluationInput(header=HEADER, tables={"lines": rows})
    rules = [
        rule(
            "derive_line_total",
            const(False),
            severity="info",
            action="annotate",
            scope={"table": "lines"},
            derive=[
                {
                    "field": "lines.line_total",
                    "expression": {
                        "op": "mul",
                        "left": fld("lines.quantity"),
                        "right": fld("lines.unit_price"),
                    },
                }
            ],
        )
    ]
    result = evaluate_rule_set(rules, data)
    assert [(d.field_key, d.row_index, d.value) for d in result.derived] == [
        ("lines.line_total", 0, "450.00")
    ]


def test_determinism_same_input_same_output() -> None:
    rules = [
        rule("a", {"op": "gt", "left": fld("total_amount"), "right": const("1000")}),
        rule(
            "b",
            {"op": "is_present", "key": "lines.sku"},
            scope={"table": "lines"},
            severity="info",
            action="annotate",
        ),
    ]
    assert evaluate_rule_set(rules, DATA) == evaluate_rule_set(rules, DATA)


# -- malformed rule sets -------------------------------------------------------------


def test_malformed_rules_are_definition_errors() -> None:
    with pytest.raises(RuleDefinitionError, match="severity"):
        evaluate_rule_set(
            [{"key": "x", "severity": "fatal", "action": "block", "condition": const(True)}], DATA
        )
    with pytest.raises(RuleDefinitionError, match="action"):
        evaluate_rule_set(
            [{"key": "x", "severity": "error", "action": "explode", "condition": const(True)}], DATA
        )
    with pytest.raises(RuleDefinitionError, match="condition"):
        evaluate_rule_set([{"key": "x", "severity": "error", "action": "block"}], DATA)
    with pytest.raises(RuleDefinitionError, match="unknown op"):
        evaluate_expression({"op": "eval", "code": "os.system"}, DATA)
    with pytest.raises(RuleDefinitionError, match="scope"):
        evaluate_rule_set([rule("x", const(True), scope={"rows": "lines"})], DATA)


# -- complexity limits ---------------------------------------------------------------


def test_depth_bomb_is_refused() -> None:
    node: dict[str, Any] = const(True)
    for _ in range(200):
        node = {"op": "not", "arg": node}
    with pytest.raises(RuleComplexityError, match="depth"):
        evaluate_expression(node, DATA)


def test_node_bomb_is_refused() -> None:
    leaf = {"op": "is_present", "key": "po_number"}
    wide = {"op": "and", "args": [dict(leaf) for _ in range(500)]}
    with pytest.raises(RuleComplexityError, match="node limit"):
        evaluate_expression(wide, DATA)


def test_rule_count_and_row_count_limits() -> None:
    many = [rule(f"r{i}", const(False)) for i in range(11)]
    with pytest.raises(RuleComplexityError, match="rules"):
        evaluate_rule_set(many, DATA, limits=EvaluationLimits(max_rules=10))
    big_table = EvaluationInput(header={}, tables={"lines": [{} for _ in range(11)]})
    with pytest.raises(RuleComplexityError, match="rows"):
        evaluate_rule_set(
            [rule("x", const(False))], big_table, limits=EvaluationLimits(max_rows_per_table=10)
        )


def test_step_budget_stops_runaway_evaluation() -> None:
    rows = [{"lines.line_total": "1"} for _ in range(100)]
    data = EvaluationInput(header={}, tables={"lines": rows})
    rules = [
        rule(
            f"sum{i}",
            {"op": "gt", "left": {"op": "sum", "key": "lines.line_total"}, "right": const("1000")},
            severity="info",
            action="annotate",
        )
        for i in range(10)
    ]
    with pytest.raises(RuleComplexityError, match="step budget"):
        evaluate_rule_set(rules, data, limits=EvaluationLimits(step_budget=500))


def test_huge_literals_do_not_explode_arithmetic() -> None:
    # A 40+ digit literal is refused as a number: indeterminate, no blowup.
    huge = "9" * 60
    value, trace = evaluate_expression(
        {"op": "mul", "left": const(huge), "right": const(huge)}, DATA
    )
    assert value is None
    assert trace["indeterminate"] is True
    # Overflow under the bounded context is indeterminate, not a crash.
    big = "9e6000"
    value, _ = evaluate_expression({"op": "mul", "left": const(big), "right": const(big)}, DATA)
    assert value is None


def test_exact_decimal_arithmetic_no_float_drift() -> None:
    value, _ = evaluate_expression({"op": "add", "left": const("0.1"), "right": const("0.2")}, DATA)
    assert value == Decimal("0.3")
