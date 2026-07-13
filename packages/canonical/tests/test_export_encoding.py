"""Golden export tests (EXP-006): exact expected bytes for the JSON and
CSV encodings — determinism, decimal/date exactness, and metadata."""

import pytest

from soa_canonical.export_encoding import (
    ExportEncodingError,
    ExportMetadata,
    encode_csv_export,
    encode_json_export,
)

METADATA = ExportMetadata(
    schema_version="1.0.0",
    mapping_version_number=2,
    integration_slug="erp",
    business_key="export:i:d:r",
    document_id="8a111111-1111-4111-8111-111111111111",
    run_id="a2222222-2222-4222-8222-222222222222",
    exported_at="2026-07-13T08:00:00+00:00",
)

PAYLOAD = {
    "PoNumber": "PO-100042",
    "OrderDate": "03/14/2026",
    "Total": "1234.50",
    "Buyer": "Büro Müller GmbH",
    "Lines": [
        {"Sku": "WID-100", "Qty": "10.000", "Amount": "450.00"},
        {"Sku": "GAD-205", "Qty": "3.000", "Amount": "784.50", "Note": 'has "quotes", too'},
    ],
}

GOLDEN_JSON = """{
  "metadata": {
    "business_key": "export:i:d:r",
    "document_id": "8a111111-1111-4111-8111-111111111111",
    "exported_at": "2026-07-13T08:00:00+00:00",
    "integration_slug": "erp",
    "mapping_version_number": 2,
    "run_id": "a2222222-2222-4222-8222-222222222222",
    "schema_version": "1.0.0"
  },
  "payload": {
    "Buyer": "Büro Müller GmbH",
    "Lines": [
      {
        "Amount": "450.00",
        "Qty": "10.000",
        "Sku": "WID-100"
      },
      {
        "Amount": "784.50",
        "Note": "has \\"quotes\\", too",
        "Qty": "3.000",
        "Sku": "GAD-205"
      }
    ],
    "OrderDate": "03/14/2026",
    "PoNumber": "PO-100042",
    "Total": "1234.50"
  }
}
""".encode()

GOLDEN_CSV = (
    "export_business_key,export_schema_version,export_mapping_version,"
    "PoNumber,OrderDate,Total,Buyer,Sku,Qty,Amount,Note\n"
    "export:i:d:r,1.0.0,2,PO-100042,03/14/2026,1234.50,Büro Müller GmbH,"
    "WID-100,10.000,450.00,\n"
    "export:i:d:r,1.0.0,2,PO-100042,03/14/2026,1234.50,Büro Müller GmbH,"
    'GAD-205,3.000,784.50,"has ""quotes"", too"\n'
).encode()


def test_golden_json_export() -> None:
    assert encode_json_export(PAYLOAD, METADATA) == GOLDEN_JSON
    # Deterministic: byte-identical across calls.
    assert encode_json_export(PAYLOAD, METADATA) == encode_json_export(PAYLOAD, METADATA)


def test_golden_csv_export() -> None:
    assert encode_csv_export(PAYLOAD, METADATA, lines_key="Lines") == GOLDEN_CSV
    assert encode_csv_export(PAYLOAD, METADATA, lines_key="Lines") == encode_csv_export(
        PAYLOAD, METADATA, lines_key="Lines"
    )


def test_decimals_and_dates_stay_exact_strings() -> None:
    """No float round-trips anywhere: '1234.50' keeps its trailing zero,
    '10.000' keeps its scale, and reformatted dates pass through."""
    json_bytes = encode_json_export(PAYLOAD, METADATA)
    assert b'"1234.50"' in json_bytes
    assert b'"10.000"' in json_bytes
    csv_bytes = encode_csv_export(PAYLOAD, METADATA, lines_key="Lines")
    assert b"1234.50" in csv_bytes and b"1234.5," not in csv_bytes
    assert b"10.000" in csv_bytes


def test_csv_without_lines_is_a_named_error_and_empty_lines_still_export_header_row() -> None:
    with pytest.raises(ExportEncodingError, match="'Lines'"):
        encode_csv_export({"PoNumber": "PO-1"}, METADATA, lines_key="Lines")
    empty = encode_csv_export({"PoNumber": "PO-1", "Lines": []}, METADATA, lines_key="Lines")
    lines = empty.decode("utf-8").splitlines()
    assert len(lines) == 2  # header + the single header-fields row
    assert lines[1].startswith("export:i:d:r,1.0.0,2,PO-1")


def test_nested_values_become_compact_deterministic_json_cells() -> None:
    payload = {
        "Extra": {"b": 1, "a": 2},
        "Lines": [{"Sku": "X"}],
    }
    encoded = encode_csv_export(payload, METADATA, lines_key="Lines")
    assert b'"{""a"":2,""b"":1}"' in encoded
