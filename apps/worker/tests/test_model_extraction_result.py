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
