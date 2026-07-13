"""Mock extraction provider (PRC-006): contract conformance plus the
mock-specific guarantees — known fixture values, honest absence for
unknown documents, error/low-confidence modes, and no network."""

import socket

import pytest

from soa_worker.extraction.contract import ExtractionProviderContract
from soa_worker.extraction.mock import (
    SYNTHETIC_SALES_ORDER,
    SYNTHETIC_SALES_ORDER_SHA256,
    MockExtractionProvider,
    MockMode,
    synthetic_sales_order_request,
)
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
)


class TestMockProviderContract(ExtractionProviderContract):
    async def make_provider(self) -> ExtractionProvider:
        return MockExtractionProvider()

    def contract_request(self) -> ExtractionRequest:
        return synthetic_sales_order_request()


def _values(result: ExtractionResult) -> dict[tuple[str, int | None], str | None]:
    return {(f.field_key, f.row_index): f.raw_value for f in result.fields}


async def test_synthetic_fixture_yields_its_known_values() -> None:
    result = await MockExtractionProvider().extract(synthetic_sales_order_request())
    values = _values(result)
    assert values[("po_number", None)] == "PO-100042"
    assert values[("order_date", None)] == "03/14/2026"
    assert values[("currency", None)] == "USD"
    assert values[("total_amount", None)] == "1,234.50"
    # Table cells carry row identity.
    assert values[("lines.sku", 0)] == "WID-100"
    assert values[("lines.sku", 1)] == "GAD-205"
    assert values[("lines.quantity", 1)] == "3"
    # The deliberately-missing field is absent, not invented.
    assert values[("delivery_terms", None)] is None
    # The table container itself is never a value.
    assert ("lines", None) not in values
    assert result.warnings == ()


async def test_fixture_hash_matches_the_shipped_bytes() -> None:
    import hashlib

    assert hashlib.sha256(SYNTHETIC_SALES_ORDER).hexdigest() == SYNTHETIC_SALES_ORDER_SHA256


async def test_unknown_document_is_all_absent_with_a_warning() -> None:
    request = synthetic_sales_order_request()
    unknown = ExtractionRequest(
        document_id=request.document_id,
        document_sha256="0" * 64,
        content_type=request.content_type,
        pages=request.pages,
        fields=request.fields,
    )
    result = await MockExtractionProvider().extract(unknown)
    assert all(f.raw_value is None for f in result.fields)
    assert all(f.evidence == () for f in result.fields)
    assert any("not a registered mock fixture" in w for w in result.warnings)


async def test_low_confidence_mode_scales_every_confidence_down() -> None:
    request = synthetic_sales_order_request()
    normal = await MockExtractionProvider().extract(request)
    low = await MockExtractionProvider(mode=MockMode.LOW_CONFIDENCE).extract(request)
    normal_by_key = {(f.field_key, f.row_index): f for f in normal.fields}
    for extracted in low.fields:
        if extracted.raw_value is None:
            continue
        counterpart = normal_by_key[(extracted.field_key, extracted.row_index)]
        assert extracted.confidence == pytest.approx(counterpart.confidence * 0.35)
        assert extracted.confidence < 0.5, "low-confidence mode must land under review gates"
        # Values and evidence are unchanged — only the signal weakens.
        assert extracted.raw_value == counterpart.raw_value
        assert extracted.evidence == counterpart.evidence


@pytest.mark.parametrize(
    ("mode", "retryable"),
    [(MockMode.ERROR_RETRYABLE, True), (MockMode.ERROR_TERMINAL, False)],
)
async def test_error_modes_raise_classified_provider_errors(
    mode: MockMode, retryable: bool
) -> None:
    with pytest.raises(ExtractionProviderError) as error:
        await MockExtractionProvider(mode=mode).extract(synthetic_sales_order_request())
    assert error.value.retryable is retryable


async def test_candidates_are_surfaced_for_ambiguous_fields() -> None:
    result = await MockExtractionProvider().extract(synthetic_sales_order_request())
    po = next(f for f in result.fields if f.field_key == "po_number")
    assert [c.raw_value for c in po.candidates] == ["PO-1000A2"]
    assert all(c.confidence < po.confidence for c in po.candidates)


async def test_extraction_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the mock provider opened a socket")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    result = await MockExtractionProvider().extract(synthetic_sales_order_request())
    assert any(f.raw_value is not None for f in result.fields)


async def test_results_are_byte_identical_across_instances_and_calls() -> None:
    request = synthetic_sales_order_request()
    provider = MockExtractionProvider()
    results = [
        await provider.extract(request),
        await provider.extract(request),
        await MockExtractionProvider().extract(request),
    ]
    assert results[0] == results[1] == results[2]
