"""Mapper tests (CAN-003): fixture mappings including line items,
provenance references, currency fallback, and named errors for
everything that cannot be placed."""

import pytest

from soa_canonical import validate_order
from soa_canonical.mapping import (
    CanonicalMappingError,
    CatalogReference,
    SourceValue,
    map_sales_order,
)

DOC = "8a111111-1111-4111-8111-111111111111"
RUN = "a2222222-2222-4222-8222-222222222222"


def header_fixture() -> dict[str, SourceValue]:
    return {
        "po_number": SourceValue(
            "PO-100042", origin="corrected", actor="user:u-1", quote="PO-100042"
        ),
        "order_date": SourceValue("2026-03-14", page_number=1, quote="March 14, 2026"),
        "customer_name": SourceValue("Acme Industrial"),
        "currency": SourceValue("USD"),
        "total_amount": SourceValue({"amount": "1234.50", "currency": "USD"}, page_number=2),
    }


def lines_fixture() -> list[dict[str, SourceValue]]:
    return [
        {
            "lines.sku": SourceValue("WID-100", page_number=1, quote="WID-100"),
            "lines.description": SourceValue("Widget, 10mm"),
            "lines.quantity": SourceValue("10", page_number=1),
            "lines.unit_price": SourceValue({"amount": "45.00", "currency": None}),
            "lines.line_total": SourceValue({"amount": "450.00", "currency": "USD"}),
        },
        {
            "lines.sku": SourceValue("GAD-205"),
            "lines.quantity": SourceValue("3", origin="corrected", actor="user:u-1"),
            "lines.line_total": SourceValue("784.50"),  # bare decimal: currency falls back
        },
    ]


def test_full_fixture_maps_with_provenance() -> None:
    order = map_sales_order(
        header=header_fixture(),
        lines=lines_fixture(),
        document_id=DOC,
        run_id=RUN,
        document_sha256="d" * 64,
        received_at="2026-03-14T09:20:00+00:00",
    )
    validate_order(dict(order))

    assert order["identifiers"]["po_number"] == "PO-100042"
    assert order["dates"]["order_date"] == "2026-03-14"
    assert order["terms"]["currency"] == "USD"
    assert order["totals"]["grand_total"] == {"amount": "1234.50", "currency": "USD"}
    assert order["parties"]["buyer"]["name"] == "Acme Industrial"  # type: ignore[index]

    first, second = order["line_items"]
    assert first["line_number"] == 1
    assert first["sku"] == "WID-100"
    assert first["unit_price"] == {"amount": "45.00", "currency": "USD"}  # fallback currency
    assert second["line_number"] == 2
    assert second["line_total"] == {"amount": "784.50", "currency": "USD"}  # bare decimal
    assert "unit_price" not in second

    provenance = order["provenance"]
    assert provenance["identifiers.po_number"] == {
        "origin": "corrected",
        "quote": "PO-100042",
        "actor": "user:u-1",
    }
    assert provenance["dates.order_date"]["page_number"] == 1
    assert provenance["line_items[1].quantity"]["origin"] == "corrected"
    assert "line_items[1].unit_price" not in provenance  # absent value, no provenance

    assert order["source"] == {
        "document_id": DOC,
        "run_id": RUN,
        "document_sha256": "d" * 64,
        "received_at": "2026-03-14T09:20:00+00:00",
    }


def test_catalog_identities_become_neutral_master_ids_and_trace_extensions() -> None:
    customer = CatalogReference(
        catalog_id="11111111-1111-4111-8111-111111111111",
        catalog_version_id="22222222-2222-4222-8222-222222222222",
        catalog_record_id="33333333-3333-4333-8333-333333333333",
        source_id="CUST-100",
        display_name="Acme Master Name",
    )
    material = CatalogReference(
        catalog_id="44444444-4444-4444-8444-444444444444",
        catalog_version_id="55555555-5555-4555-8555-555555555555",
        catalog_record_id="66666666-6666-4666-8666-666666666666",
        source_id="SKU-900",
        display_name="Widget 900",
    )
    header = header_fixture()
    header["customer_name"] = SourceValue("acme alias", catalog=customer)
    lines = lines_fixture()
    lines[0]["lines.sku"] = SourceValue("widget nine hundred", catalog=material)

    order = map_sales_order(header=header, lines=lines, document_id=DOC, run_id=RUN)

    assert order["parties"]["buyer"] == {  # type: ignore[index]
        "name": "Acme Master Name",
        "identifiers": [{"scheme": "customer-account", "value": "CUST-100"}],
    }
    assert order["line_items"][0]["sku"] == "SKU-900"
    extension = order["extensions"]["x_soa_catalog"]  # type: ignore[index]
    assert extension["customer"]["catalog_record_id"] == customer.catalog_record_id
    assert extension["line_items"][0]["catalog_version_id"] == material.catalog_version_id
    validate_order(dict(order))


def test_every_missing_required_value_is_named() -> None:
    with pytest.raises(CanonicalMappingError) as excinfo:
        map_sales_order(
            header={"customer_name": SourceValue("Acme")},
            lines=[{"lines.sku": SourceValue("WID-100")}],
            document_id=DOC,
            run_id=RUN,
        )
    joined = "\n".join(excinfo.value.errors)
    for path in (
        "identifiers.po_number",
        "dates.order_date",
        "terms.currency",
        "totals.grand_total",
        "line_items[0].quantity",
        "line_items[0].line_total",
    ):
        assert path in joined, f"{path} missing from: {joined}"


def test_no_lines_is_an_error() -> None:
    with pytest.raises(CanonicalMappingError, match="no line items"):
        map_sales_order(header=header_fixture(), lines=[], document_id=DOC, run_id=RUN)


def test_money_without_any_currency_is_an_error() -> None:
    header = header_fixture()
    del header["currency"]
    header["total_amount"] = SourceValue({"amount": "10.00", "currency": None})
    with pytest.raises(CanonicalMappingError) as excinfo:
        map_sales_order(header=header, lines=lines_fixture(), document_id=DOC, run_id=RUN)
    joined = "\n".join(excinfo.value.errors)
    assert "terms.currency" in joined
    assert "no currency" in joined


def test_schema_violations_become_mapping_errors() -> None:
    """Values the schema refuses (a locale-formatted decimal, a US date)
    surface as mapping errors with their JSON paths — the mapper can
    never hand out an invalid payload."""
    header = header_fixture()
    header["order_date"] = SourceValue("03/14/2026")
    header["total_amount"] = SourceValue({"amount": "1,234.50", "currency": "USD"})
    with pytest.raises(CanonicalMappingError) as excinfo:
        map_sales_order(header=header, lines=lines_fixture(), document_id=DOC, run_id=RUN)
    joined = "\n".join(excinfo.value.errors)
    assert "order_date" in joined
    assert "grand_total" in joined
