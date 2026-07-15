"""Output-bound tests for the shared model-output parser (AIO-008).

``parse_model_extraction`` caps the number of field entries and the
length of any single value, independent of whether the provider honored
``max_tokens``. An oversized response is rejected as invalid so it flows
through the AIO-012 repair path instead of landing an unbounded row in
the database.
"""

import json
import uuid

import pytest

from soa_worker.extraction.provider import ExtractionRequest, FieldSpec, PageInput
from soa_worker.model_extraction_result import (
    MAX_FIELD_ENTRIES,
    MAX_VALUE_LENGTH,
    ModelOutputInvalidError,
    parse_model_extraction,
)
from soa_worker.model_request_builder import BuiltModelRequest

DOC_ID = uuid.UUID("6a2c7b00-0000-4000-8000-0000000000ff")


def _built() -> BuiltModelRequest:
    return BuiltModelRequest(
        messages=(),
        instruction_reference=None,
        pages_included=1,
        pages_dropped=0,
        chars_truncated=0,
        redaction_applied=False,
        warnings=(),
    )


def _request() -> ExtractionRequest:
    return ExtractionRequest(
        document_id=DOC_ID,
        document_sha256="f" * 64,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=1000, height_px=1400),),
        fields=(FieldSpec(key="po_number", field_type="text"),),
    )


def _parse(content: str) -> object:
    return parse_model_extraction(
        _request(), content, _built(), provider="test", model="test-model", cost_cents=7
    )


def test_too_many_field_entries_is_invalid() -> None:
    entries = [{"key": "po_number", "value": "x"} for _ in range(MAX_FIELD_ENTRIES + 1)]
    with pytest.raises(ModelOutputInvalidError) as exc:
        _parse(json.dumps({"fields": entries}))
    assert str(MAX_FIELD_ENTRIES) in exc.value.reason
    # The failed call's cost still travels back to the repair policy.
    assert exc.value.cost_cents == 7


def test_oversized_value_is_invalid() -> None:
    entry = {"key": "po_number", "value": "A" * (MAX_VALUE_LENGTH + 1)}
    with pytest.raises(ModelOutputInvalidError) as exc:
        _parse(json.dumps({"fields": [entry]}))
    assert str(MAX_VALUE_LENGTH) in exc.value.reason
    # The reason is safe to log and to re-ask the model with — it carries
    # no customer data.
    assert "A" * 20 not in exc.value.reason


def _table_request() -> ExtractionRequest:
    """A request with a ``lines`` table and its columns, like the sales-order
    schema."""
    return ExtractionRequest(
        document_id=DOC_ID,
        document_sha256="f" * 64,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=1000, height_px=1400),),
        fields=(
            FieldSpec(key="po_number", field_type="text"),
            FieldSpec(key="lines", field_type="table"),
            FieldSpec(key="lines.sku", field_type="text"),
            FieldSpec(key="lines.quantity", field_type="number"),
            FieldSpec(key="lines.unit_price", field_type="money"),
        ),
    )


def test_a_nested_table_array_is_flattened_into_row_indexed_columns() -> None:
    # The natural shape a real model emits: one "lines" entry whose value is
    # an array of row objects, rather than separate lines.sku + row_index
    # entries. Both must capture.
    content = json.dumps(
        {
            "fields": [
                {"key": "po_number", "value": "PO-1"},
                {
                    "key": "lines",
                    "confidence": 0.9,
                    "value": [
                        {"sku": "WID-1", "quantity": 10, "unit_price": 5.0, "vat": "ignored"},
                        {"sku": "GAD-2", "quantity": 3, "unit_price": None},
                    ],
                },
            ]
        }
    )
    result = parse_model_extraction(_table_request(), content, _built(), provider="test", model="m")
    cells = {(f.field_key, f.row_index): f.raw_value for f in result.fields}
    # Row 0 columns captured with the right row index.
    assert cells[("lines.sku", 0)] == "WID-1"
    assert cells[("lines.quantity", 0)] == "10"
    assert cells[("lines.unit_price", 0)] == "5.0"
    # Row 1 captured; a null cell is absent, not a "None" string.
    assert cells[("lines.sku", 1)] == "GAD-2"
    assert ("lines.unit_price", 1) not in cells
    # An unrequested sub-key ("vat") is dropped, never invented.
    assert not any(k.endswith(".vat") for k, _ in cells)


def test_flat_line_item_entries_still_capture() -> None:
    # A model that DOES emit the flat form is unaffected.
    content = json.dumps(
        {
            "fields": [
                {"key": "lines.sku", "value": "WID-1", "row_index": 0},
                {"key": "lines.quantity", "value": "10", "row_index": 0},
            ]
        }
    )
    result = parse_model_extraction(_table_request(), content, _built(), provider="test", model="m")
    cells = {(f.field_key, f.row_index): f.raw_value for f in result.fields}
    assert cells[("lines.sku", 0)] == "WID-1"
    assert cells[("lines.quantity", 0)] == "10"


def test_output_exactly_at_the_caps_is_accepted() -> None:
    entry = {
        "key": "po_number",
        "value": "A" * MAX_VALUE_LENGTH,
        "confidence": 0.9,
        "page_number": 1,
    }
    result = parse_model_extraction(
        _request(), json.dumps({"fields": [entry]}), _built(), provider="test", model="m"
    )
    by_key = {field.field_key: field for field in result.fields}
    assert by_key["po_number"].raw_value == "A" * MAX_VALUE_LENGTH
