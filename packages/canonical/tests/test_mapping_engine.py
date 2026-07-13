"""Mapping engine tests (EXP-002): the transform matrix, conditions and
defaults, constants, line fan-out, the trace, malformed definitions,
bounded execution, and target-schema validation."""

from typing import Any

import pytest

from soa_canonical.mapping_engine import (
    MAX_LINE_ITEMS,
    MAX_TRACED_ROWS,
    MappingDefinitionError,
    MappingExecutionError,
    execute_mapping,
    validate_mapping_definition,
)

CANONICAL: dict[str, Any] = {
    "schema_version": "1.0.0",
    "identifiers": {"po_number": "po-100042"},
    "dates": {"order_date": "2026-03-14"},
    "terms": {"currency": "USD"},
    "totals": {"grand_total": {"amount": "1234.5", "currency": "USD"}},
    "line_items": [
        {
            "line_number": 1,
            "sku": "WID-100",
            "quantity": "10",
            "unit_of_measure": "each",
            "line_total": {"amount": "450.00", "currency": "USD"},
        },
        {
            "line_number": 2,
            "quantity": "3",
            "line_total": {"amount": "784.5", "currency": "USD"},
        },
    ],
    "source": {"document_id": "d", "run_id": "r"},
}


def test_transform_matrix_with_trace() -> None:
    result = execute_mapping(
        {
            "fields": [
                {
                    "target": "PoNumber",
                    "source": "identifiers.po_number",
                    "format": {"kind": "uppercase"},
                    "required": True,
                },
                {
                    "target": "OrderDate",
                    "source": "dates.order_date",
                    "format": {"kind": "date", "pattern": "MM/DD/YYYY"},
                },
                {
                    "target": "Total",
                    "source": "totals.grand_total.amount",
                    "format": {"kind": "decimal", "scale": 2},
                },
                {
                    "target": "Reference",
                    "source": "identifiers.po_number",
                    "format": {"kind": "prefix", "value": "SOA-"},
                },
                # Missing source -> default.
                {"target": "Warehouse", "source": "extensions.x_zone", "default": "MAIN"},
                # Condition false -> omitted entirely.
                {
                    "target": "Rush",
                    "source": "identifiers.po_number",
                    "when": {"present": "extensions.x_rush"},
                },
                # Condition true (equals) -> included.
                {
                    "target": "CurrencyOk",
                    "source": "terms.currency",
                    "when": {"equals": {"path": "terms.currency", "value": "USD"}},
                },
            ],
            "constants": [{"target": "SourceSystem", "value": "SOA"}],
            "lines": {
                "source": "line_items",
                "target": "Lines",
                "fields": [
                    {"target": "Sku", "source": "sku", "default": "UNSPECIFIED"},
                    {
                        "target": "Qty",
                        "source": "quantity",
                        "format": {"kind": "decimal", "scale": 3},
                    },
                    {
                        "target": "Uom",
                        "source": "unit_of_measure",
                        # format.default covers UNKNOWN values; the field
                        # default covers ABSENT ones.
                        "format": {"kind": "map", "values": {"each": "EA"}, "default": "EA"},
                        "default": "EA",
                    },
                    {"target": "Amount", "source": "line_total.amount"},
                ],
            },
        },
        CANONICAL,
    )
    assert result.payload == {
        "PoNumber": "PO-100042",
        "OrderDate": "03/14/2026",
        "Total": "1234.50",
        "Reference": "SOA-po-100042",
        "Warehouse": "MAIN",
        "CurrencyOk": "USD",
        "SourceSystem": "SOA",
        "Lines": [
            {"Sku": "WID-100", "Qty": "10.000", "Uom": "EA", "Amount": "450.00"},
            {"Sku": "UNSPECIFIED", "Qty": "3.000", "Uom": "EA", "Amount": "784.5"},
        ],
    }
    by_target = {entry.target: entry for entry in result.trace if entry.target != "Sku"}
    assert by_target["PoNumber"].applied == ("uppercase",)
    assert by_target["PoNumber"].raw == "po-100042"
    assert by_target["Warehouse"].used_default is True
    assert by_target["Rush"].condition_passed is False
    assert "Rush" not in result.payload
    assert by_target["SourceSystem"].source is None
    # Line rows are traced too (both rows fit under the cap).
    sku_entries = [entry for entry in result.trace if entry.target == "Sku"]
    assert len(sku_entries) == 2
    assert sku_entries[1].used_default is True


def test_malformed_definitions_name_every_problem() -> None:
    errors = validate_mapping_definition(
        {
            "fields": [
                {"target": "", "source": "identifiers.po_number"},
                {"target": "X", "source": "a.b", "format": {"kind": "python_eval"}},
                {"target": "Y", "source": "a.b", "when": {"maybe": True}},
                {"target": "Z", "source": "a.b", "unknown_key": 1},
            ],
            "constants": [{"target": "C"}],
            "lines": {"source": "rows", "target": "L", "fields": []},
            "run_code": "import os",
        }
    )
    joined = "\n".join(errors)
    assert "'target' must be a non-empty string" in joined
    assert "python_eval" in joined and "kinds:" in joined
    assert "'when' supports" in joined
    assert "unknown keys ['unknown_key']" in joined
    assert "needs exactly {target, value}" in joined
    assert "only 'line_items'" in joined
    assert "non-empty list" in joined
    assert "run_code" in joined

    with pytest.raises(MappingDefinitionError):
        execute_mapping({"run_code": "import os"}, CANONICAL)


def test_execution_errors_are_named_not_guessed() -> None:
    # A required field with no value, and an unformattable decimal.
    with pytest.raises(MappingExecutionError) as excinfo:
        execute_mapping(
            {
                "fields": [
                    {"target": "Missing", "source": "identifiers.nope", "required": True},
                    {
                        "target": "Bad",
                        "source": "identifiers.po_number",
                        "format": {"kind": "decimal", "scale": 2},
                    },
                    {
                        "target": "NoMap",
                        "source": "terms.currency",
                        "format": {"kind": "map", "values": {"EUR": "E"}},
                    },
                ]
            },
            CANONICAL,
        )
    joined = "\n".join(excinfo.value.errors)
    assert "required source 'identifiers.nope' has no value" in joined
    assert "is not a decimal" in joined
    assert "has no mapping" in joined


def test_bounded_execution() -> None:
    big = dict(CANONICAL)
    big["line_items"] = [
        {"line_number": index + 1, "quantity": "1", "line_total": {"amount": "1"}}
        for index in range(MAX_LINE_ITEMS + 1)
    ]
    definition = {
        "lines": {
            "source": "line_items",
            "target": "Lines",
            "fields": [{"target": "Qty", "source": "quantity"}],
        }
    }
    with pytest.raises(MappingExecutionError, match="exceeds the"):
        execute_mapping(definition, big)

    # Under the row cap the mapping runs, and the TRACE cap is stated.
    big["line_items"] = big["line_items"][: MAX_TRACED_ROWS + 10]
    result = execute_mapping(definition, big)
    assert len(result.payload["Lines"]) == MAX_TRACED_ROWS + 10
    assert len([e for e in result.trace if e.target == "Qty"]) == MAX_TRACED_ROWS
    assert any("trace covers the first" in note for note in result.notes)


def test_target_schema_validation_blocks_bad_payloads() -> None:
    definition = {"fields": [{"target": "PoNumber", "source": "identifiers.po_number"}]}
    target_schema = {
        "type": "object",
        "required": ["PoNumber", "OrderDate"],
        "properties": {"PoNumber": {"type": "string"}},
    }
    with pytest.raises(MappingExecutionError, match="OrderDate"):
        execute_mapping(definition, CANONICAL, target_schema=target_schema)
    # With the field supplied, the same schema passes.
    definition["fields"].append({"target": "OrderDate", "source": "dates.order_date"})
    result = execute_mapping(definition, CANONICAL, target_schema=target_schema)
    assert result.payload["OrderDate"] == "2026-03-14"


def test_execution_is_deterministic() -> None:
    definition = {
        "fields": [
            {
                "target": "Total",
                "source": "totals.grand_total.amount",
                "format": {"kind": "decimal", "scale": 2},
            }
        ]
    }
    first = execute_mapping(definition, CANONICAL)
    second = execute_mapping(definition, CANONICAL)
    assert first.payload == second.payload
    assert [e.to_json() for e in first.trace] == [e.to_json() for e in second.trace]
