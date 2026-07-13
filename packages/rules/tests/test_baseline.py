"""Baseline sales-order rules (PRC-010): a clean order passes, each
defect fires exactly its rule, tolerances behave as published."""

from typing import Any

from soa_rules import EvaluationInput, evaluate_rule_set
from soa_rules.baseline import (
    BASELINE_VERSION,
    baseline_sales_order_rule_set,
    baseline_sales_order_rules,
)


def clean_order(**overrides: Any) -> EvaluationInput:
    header: dict[str, Any] = {
        "po_number": "PO-100042",
        "order_date": "2026-03-14",
        "requested_delivery_date": "2026-04-01",
        "customer_name": "Acme Industrial Supply",
        "currency": "USD",
        "total_amount": {"amount": "1234.50", "currency": "USD"},
    }
    lines: list[dict[str, Any]] = [
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
    tables = {"lines": overrides.pop("lines", lines)}
    header.update(overrides)
    return EvaluationInput(header=header, tables=tables)


def triggered_keys(data: EvaluationInput) -> set[tuple[str, int | None]]:
    result = evaluate_rule_set(baseline_sales_order_rules(), data)
    return {(f.rule_key, f.row_index) for f in result.triggered()}


def test_rule_set_is_versioned_with_explicit_tolerances() -> None:
    rule_set = baseline_sales_order_rule_set()
    assert rule_set["version"] == BASELINE_VERSION
    line_rule = next(
        r for r in rule_set["rules"] if r["key"] == "lines.total_matches_quantity_times_price"
    )
    condition = line_rule["condition"]["arg"]
    assert condition["tolerance"] == {"op": "const", "value": "0.01"}
    header_rule = next(r for r in rule_set["rules"] if r["key"] == "totals.header_matches_lines")
    reconciliation = header_rule["condition"]["arg"]
    assert reconciliation["tolerance"] == {"op": "const", "value": "0.05"}
    assert reconciliation["relative_tolerance"] == {"op": "const", "value": "0.0005"}


def test_clean_order_triggers_nothing_and_does_not_block() -> None:
    result = evaluate_rule_set(baseline_sales_order_rules(), clean_order())
    assert result.triggered() == ()
    assert result.blocking is False
    assert result.review_required is False


def test_missing_critical_fields_block() -> None:
    fired = triggered_keys(clean_order(po_number=None))
    assert ("required.po_number", None) in fired
    result = evaluate_rule_set(baseline_sales_order_rules(), clean_order(po_number=None))
    assert result.blocking is True


def test_missing_lines_block() -> None:
    assert ("lines.present", None) in triggered_keys(clean_order(lines=[]))


def test_delivery_before_order_date_fires() -> None:
    fired = triggered_keys(clean_order(requested_delivery_date="2026-03-01"))
    assert ("dates.delivery_after_order", None) in fired


def test_missing_delivery_date_passes_the_date_rule() -> None:
    # The delivery date is optional: without it the guarded rule PASSES
    # (not indeterminate), so the document is not parked in review.
    result = evaluate_rule_set(
        baseline_sales_order_rules(), clean_order(requested_delivery_date=None)
    )
    finding = next(f for f in result.findings if f.rule_key == "dates.delivery_after_order")
    assert finding.status == "passed"


def test_zero_quantity_and_negative_price_fire_on_their_row() -> None:
    lines = [
        {
            "lines.sku": "WID-100",
            "lines.quantity": "0",
            "lines.unit_price": "-45.00",
            "lines.line_total": "0.00",
        },
        {
            "lines.sku": "GAD-205",
            "lines.quantity": "3",
            "lines.unit_price": "261.50",
            "lines.line_total": "784.50",
        },
    ]
    fired = triggered_keys(
        clean_order(lines=lines, total_amount={"amount": "784.50", "currency": "USD"})
    )
    assert ("lines.quantity_positive", 0) in fired
    assert ("lines.unit_price_not_negative", 0) in fired
    assert ("lines.quantity_positive", 1) not in fired


def test_line_total_tolerance_is_exactly_one_cent() -> None:
    within = clean_order(
        lines=[
            {
                "lines.sku": "W",
                "lines.quantity": "10",
                "lines.unit_price": "45.00",
                "lines.line_total": "450.01",  # off by exactly the tolerance
            }
        ],
        total_amount={"amount": "450.01", "currency": "USD"},
    )
    assert ("lines.total_matches_quantity_times_price", 0) not in triggered_keys(within)
    beyond = clean_order(
        lines=[
            {
                "lines.sku": "W",
                "lines.quantity": "10",
                "lines.unit_price": "45.00",
                "lines.line_total": "450.02",  # a cent past the tolerance
            }
        ],
        total_amount={"amount": "450.02", "currency": "USD"},
    )
    assert ("lines.total_matches_quantity_times_price", 0) in triggered_keys(beyond)


def test_missing_line_total_is_derived_not_blocked() -> None:
    data = clean_order(
        lines=[
            {"lines.sku": "W", "lines.quantity": "10", "lines.unit_price": "45.00"},
        ],
        total_amount={"amount": "450.00", "currency": "USD"},
    )
    result = evaluate_rule_set(baseline_sales_order_rules(), data)
    assert ("lines.total_matches_quantity_times_price", 0) not in {
        (f.rule_key, f.row_index) for f in result.triggered()
    }
    assert [(d.field_key, d.row_index, d.value) for d in result.derived] == [
        ("lines.line_total", 0, "450.00")
    ]


def test_header_reconciliation_fires_beyond_published_slack() -> None:
    # Lines sum to 1234.50; slack is 0.05 + 0.0005 * 1234.55... ~ 0.67.
    within = clean_order(total_amount={"amount": "1235.00", "currency": "USD"})
    assert ("totals.header_matches_lines", None) not in triggered_keys(within)
    beyond = clean_order(total_amount={"amount": "1236.00", "currency": "USD"})
    assert ("totals.header_matches_lines", None) in triggered_keys(beyond)


def test_currency_mismatch_routes_to_review() -> None:
    fired = triggered_keys(clean_order(total_amount={"amount": "1234.50", "currency": "EUR"}))
    assert ("currency.total_matches_header", None) in fired
    # A bare amount (no currency captured) is indeterminate, not a mismatch.
    assert ("currency.total_matches_header", None) not in triggered_keys(
        clean_order(total_amount={"amount": "1234.50", "currency": None})
    )


def test_duplicate_hook_fires_when_ingestion_flagged_one() -> None:
    fired = triggered_keys(clean_order(**{"meta.duplicate_of": "11111111-aaaa"}))
    assert ("duplicates.business_hook", None) in fired
    result = evaluate_rule_set(
        baseline_sales_order_rules(), clean_order(**{"meta.duplicate_of": "x"})
    )
    assert result.blocking is False  # duplicates route to review, not block
    assert result.review_required is True
