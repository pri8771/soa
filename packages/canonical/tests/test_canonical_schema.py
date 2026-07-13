"""Canonical order schema tests (CAN-001): valid/invalid fixtures,
explicit nullability and representations, closed objects, the extension
mechanism, and the no-ERP-leakage guarantee."""

import json
import re
from typing import Any

import pytest

from soa_canonical import (
    CURRENT_VERSION,
    CanonicalValidationError,
    UnknownSchemaVersionError,
    list_versions,
    load_schema,
    validate_order,
)

DOC_ID = "8a111111-1111-4111-8111-111111111111"
RUN_ID = "a2222222-2222-4222-8222-222222222222"


def minimal_order() -> dict[str, Any]:
    """The smallest payload the schema accepts — required fields only."""
    return {
        "schema_version": "1.0.0",
        "identifiers": {"po_number": "PO-100042"},
        "dates": {"order_date": "2026-03-14"},
        "terms": {"currency": "USD"},
        "totals": {"grand_total": {"amount": "450.00", "currency": "USD"}},
        "line_items": [
            {
                "line_number": 1,
                "quantity": "10",
                "line_total": {"amount": "450.00", "currency": "USD"},
            }
        ],
        "source": {"document_id": DOC_ID, "run_id": RUN_ID},
    }


def full_order() -> dict[str, Any]:
    """Every section populated, with explicit nulls where unknown."""
    return {
        "schema_version": "1.0.0",
        "identifiers": {
            "po_number": "PO-100042",
            "sales_order_reference": None,
            "external_references": [{"scheme": "quote", "value": "Q-2026-0117"}],
        },
        "parties": {
            "buyer": {
                "name": "Northstar Distribution",
                "address": {
                    "line1": "500 Harbor Blvd",
                    "line2": None,
                    "city": "Oakland",
                    "region": "CA",
                    "postal_code": "94607",
                    "country": "US",
                },
                "contact": {"email": "purchasing@northstar.example", "phone": None},
                "identifiers": [{"scheme": "customer-account", "value": "NS-4471"}],
            },
            "seller": {"name": "Acme Industrial Supply"},
            "ship_to": None,
            "bill_to": None,
        },
        "dates": {
            "order_date": "2026-03-14",
            "requested_delivery_date": "2026-03-28",
            "promised_delivery_date": None,
        },
        "terms": {
            "currency": "USD",
            "payment_terms": "Net 30",
            "incoterms": {"code": "DDP", "location": "Oakland, CA"},
        },
        "totals": {
            "subtotal": {"amount": "1234.50", "currency": "USD"},
            "discount_total": None,
            "tax_total": {"amount": "0", "currency": "USD"},
            "shipping_total": None,
            "grand_total": {"amount": "1234.50", "currency": "USD"},
        },
        "line_items": [
            {
                "line_number": 1,
                "sku": "WID-100",
                "description": "Widget, 10mm galvanized",
                "quantity": "10",
                "unit_of_measure": "each",
                "unit_price": {"amount": "45.00", "currency": "USD"},
                "line_total": {"amount": "450.00", "currency": "USD"},
                "requested_delivery_date": None,
                "notes": [],
            },
            {
                "line_number": 2,
                "sku": "GAD-205",
                "description": None,
                "quantity": "3",
                "unit_of_measure": "case",
                "unit_price": {"amount": "261.50", "currency": "USD"},
                "line_total": {"amount": "784.50", "currency": "USD"},
            },
        ],
        "notes": [
            {
                "text": "Customer asked for liftgate delivery.",
                "author": "user:u-1",
                "visibility": "internal",
            }
        ],
        "source": {
            "document_id": DOC_ID,
            "run_id": RUN_ID,
            "document_sha256": "d" * 64,
            "received_at": "2026-03-14T09:20:00+00:00",
        },
        "provenance": {
            "identifiers.po_number": {
                "origin": "corrected",
                "page_number": 1,
                "quote": "PO-100042",
                "actor": "user:u-1",
            },
            "line_items[0].quantity": {"origin": "extracted", "page_number": 1, "quote": "10"},
            "totals.grand_total": {"origin": "derived", "page_number": None, "quote": None},
        },
        "extensions": {"x_warehouse_zone": "B-14"},
    }


def test_valid_fixtures_pass() -> None:
    validate_order(minimal_order())
    validate_order(full_order())


def test_schema_versioning_is_explicit() -> None:
    assert list_versions() == [CURRENT_VERSION]
    assert load_schema()["$id"].endswith(f"{CURRENT_VERSION}.json")
    with pytest.raises(UnknownSchemaVersionError):
        load_schema("9.9.9")
    wrong = minimal_order()
    wrong["schema_version"] = "0.9.0"
    with pytest.raises(CanonicalValidationError, match="schema_version"):
        validate_order(wrong)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        # Required fields are explicit.
        (lambda o: o["identifiers"].pop("po_number"), "po_number"),
        (lambda o: o["dates"].pop("order_date"), "order_date"),
        (lambda o: o["totals"].pop("grand_total"), "grand_total"),
        (lambda o: o.pop("source"), "source"),
        # Decimals are exact strings — no floats, no separators.
        (
            lambda o: o["totals"].__setitem__("grand_total", {"amount": 450.0, "currency": "USD"}),
            "450.0",
        ),
        (
            lambda o: o["totals"].__setitem__(
                "grand_total", {"amount": "1,234.50", "currency": "USD"}
            ),
            "1,234.50",
        ),
        # Dates are real ISO dates.
        (lambda o: o["dates"].__setitem__("order_date", "03/14/2026"), "03/14/2026"),
        (lambda o: o["dates"].__setitem__("order_date", "2026-13-40"), "2026-13-40"),
        # Money always carries its currency, uppercase ISO 4217.
        (lambda o: o["terms"].__setitem__("currency", "usd"), "usd"),
        (lambda o: o["totals"].__setitem__("grand_total", {"amount": "450.00"}), "currency"),
        # Orders have at least one line; line numbers are 1-based.
        (lambda o: o.__setitem__("line_items", []), "non-empty"),
        (lambda o: o["line_items"][0].__setitem__("line_number", 0), "minimum"),
        # Objects are closed: unknown fields are refused, not ignored.
        (lambda o: o.__setitem__("erp_customer_code", "X1"), "erp_customer_code"),
        (lambda o: o["identifiers"].__setitem__("sap_vendor", "0042"), "sap_vendor"),
        # Extensions must be namespaced.
        (lambda o: o.__setitem__("extensions", {"warehouse_zone": "B-14"}), "warehouse_zone"),
        # Explicit nullability: null where allowed, refused where not.
        (lambda o: o["identifiers"].__setitem__("po_number", None), "po_number"),
    ],
)
def test_invalid_fixtures_fail(mutate: Any, expected: str) -> None:
    order = minimal_order()
    mutate(order)
    with pytest.raises(CanonicalValidationError) as excinfo:
        validate_order(order)
    assert expected in str(excinfo.value) or any(expected in e for e in excinfo.value.errors)


def test_every_error_is_reported_with_its_path() -> None:
    order = minimal_order()
    del order["identifiers"]["po_number"]
    order["terms"]["currency"] = "usd"
    with pytest.raises(CanonicalValidationError) as excinfo:
        validate_order(order)
    joined = "\n".join(excinfo.value.errors)
    assert "$.identifiers" in joined
    assert "$.terms.currency" in joined
    assert len(excinfo.value.errors) >= 2


def test_no_erp_specific_leakage() -> None:
    """No property name anywhere in the schema names a downstream
    system — the canonical order is ERP-neutral by construction."""
    banned = re.compile(r"sap|netsuite|oracle|dynamics|quickbooks|xero|odoo|erp", re.IGNORECASE)
    text = json.dumps(load_schema())
    names: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(value.keys())
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(load_schema())
    leaks = sorted(name for name in names if banned.search(name))
    assert leaks == [], f"ERP-specific property names in the canonical schema: {leaks}"
    # The description SELLS neutrality; make sure it is stated.
    assert "ERP-neutral" in text


def test_extensions_are_the_only_open_block() -> None:
    schema = load_schema()
    assert schema["additionalProperties"] is False
    for name, prop in schema["properties"].items():
        if name in ("extensions", "provenance"):
            continue
        if prop.get("type") == "object":
            assert prop.get("additionalProperties") is False, f"{name} must be closed"
    for name, definition in schema["$defs"].items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, f"$defs.{name} must be closed"
    # And the open block itself only accepts namespaced keys.
    valid = minimal_order()
    valid["extensions"] = {"x_custom_flag": True, "x_zone": {"aisle": 4}}
    validate_order(valid)
