"""Baseline sales-order rule set (PRC-010).

The platform's stock validation for the canonical sales-order schema
(the PRC-006 fixture keys: po_number, order_date,
requested_delivery_date, customer_name, currency, total_amount, and the
lines table). Streams with custom schemas get these semantics through
their own rule sets; this baseline is what a new stream starts from.

Tolerances and rounding are EXPLICIT and VERSIONED: values are compared
exactly as normalized (PRC-008 never rounds), and the constants below
absorb legitimate rounding drift — a cent per line, five cents or 0.05%
on the header total. Changing any constant is a new BASELINE_VERSION,
because documents validated under different slack are not comparable.

The duplicate hook: ingestion (ING-006) records business duplicates on
the document; the pipeline exposes that to rules as the
``meta.duplicate_of`` value. The baseline routes flagged duplicates to
review rather than blocking — policy can tighten it per stream.
"""

from typing import Any

#: Bump on ANY change to rules or tolerances below.
BASELINE_VERSION = "1.0.0"

#: Per-line: |quantity x unit_price - line_total| <= one cent.
LINE_TOTAL_ABS_TOLERANCE = "0.01"
#: Header: |sum(line_total) - total_amount| <= 5 cents + 0.05% of total.
HEADER_TOTAL_ABS_TOLERANCE = "0.05"
HEADER_TOTAL_REL_TOLERANCE = "0.0005"


def _field(key: str) -> dict[str, Any]:
    return {"op": "field", "key": key}


def _const(value: Any) -> dict[str, Any]:
    return {"op": "const", "value": value}


def _missing(key: str) -> dict[str, Any]:
    return {"op": "not", "arg": {"op": "is_present", "key": key}}


def _required(key: str, label: str, *, action: str) -> dict[str, Any]:
    return {
        "key": f"required.{key}",
        "severity": "error",
        "action": action,
        "condition": _missing(key),
        "message": f"{label} is required",
    }


def baseline_sales_order_rules() -> list[dict[str, Any]]:
    """The rules list, freshly built (callers may not mutate shared state)."""
    line_total_expected = {
        "op": "mul",
        "left": _field("lines.quantity"),
        "right": _field("lines.unit_price"),
    }
    return [
        # -- required critical fields ------------------------------------
        _required("po_number", "PO number", action="block"),
        _required("order_date", "Order date", action="block"),
        _required("total_amount", "Order total", action="block"),
        _required("customer_name", "Customer name", action="route_to_review"),
        _required("currency", "Currency", action="route_to_review"),
        {
            "key": "lines.present",
            "severity": "error",
            "action": "block",
            "condition": {
                "op": "eq",
                "left": {"op": "row_count", "key": "lines"},
                "right": _const(0),
            },
            "message": "The order has no line items",
        },
        # -- date order ----------------------------------------------------
        {
            "key": "dates.delivery_after_order",
            "severity": "error",
            "action": "route_to_review",
            "condition": {
                "op": "gt",
                "left": _field("order_date"),
                "right": _field("requested_delivery_date"),
            },
            "message": "Requested delivery date is before the order date",
        },
        # -- per-line quantity / price / total -------------------------------
        {
            "key": "lines.quantity_positive",
            "severity": "error",
            "action": "block",
            "scope": {"table": "lines"},
            "condition": {
                "op": "lte",
                "left": _field("lines.quantity"),
                "right": _const("0"),
            },
            "message": "Line quantity must be greater than zero",
        },
        {
            "key": "lines.unit_price_not_negative",
            "severity": "error",
            "action": "route_to_review",
            "scope": {"table": "lines"},
            "condition": {
                "op": "lt",
                "left": _field("lines.unit_price"),
                "right": _const("0"),
            },
            "message": "Line unit price is negative",
        },
        {
            "key": "lines.total_matches_quantity_times_price",
            "severity": "error",
            "action": "block",
            "scope": {"table": "lines"},
            "condition": {
                "op": "not",
                "arg": {
                    "op": "approx_eq",
                    "left": line_total_expected,
                    "right": _field("lines.line_total"),
                    "tolerance": _const(LINE_TOTAL_ABS_TOLERANCE),
                },
            },
            "message": (
                "Line total disagrees with quantity x unit price beyond the "
                f"{LINE_TOTAL_ABS_TOLERANCE} tolerance"
            ),
            # Derive the line total when the document omits it — computable
            # and absent only; extracted values are never overwritten.
            "derive": [{"field": "lines.line_total", "expression": line_total_expected}],
        },
        # -- header reconciliation --------------------------------------------
        {
            "key": "totals.header_matches_lines",
            "severity": "error",
            "action": "block",
            "condition": {
                "op": "not",
                "arg": {
                    "op": "approx_eq",
                    "left": {"op": "sum", "key": "lines.line_total"},
                    "right": _field("total_amount"),
                    "tolerance": _const(HEADER_TOTAL_ABS_TOLERANCE),
                    "relative_tolerance": _const(HEADER_TOTAL_REL_TOLERANCE),
                },
            },
            "message": (
                "Order total disagrees with the sum of line totals beyond "
                f"{HEADER_TOTAL_ABS_TOLERANCE} + {HEADER_TOTAL_REL_TOLERANCE} relative"
            ),
        },
        # -- currency consistency ----------------------------------------------
        {
            "key": "currency.total_matches_header",
            "severity": "error",
            "action": "route_to_review",
            "condition": {
                "op": "ne",
                "left": {"op": "currency_of", "arg": _field("total_amount")},
                "right": _field("currency"),
            },
            "message": "The order total's currency disagrees with the stated currency",
        },
        # -- duplicate hook ------------------------------------------------------
        {
            "key": "duplicates.business_hook",
            "severity": "warning",
            "action": "route_to_review",
            "condition": {"op": "is_present", "key": "meta.duplicate_of"},
            "message": "Ingestion flagged this document as a possible duplicate",
        },
    ]


def baseline_sales_order_rule_set() -> dict[str, Any]:
    """The versioned rule-set document: what gets pinned into a run's
    configuration and recorded alongside its findings."""
    return {"version": BASELINE_VERSION, "rules": baseline_sales_order_rules()}
