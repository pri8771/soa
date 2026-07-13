"""Line-item validation tests (CAT-012): the clean line passes, and the
matrix — item mapping, effective dates, UOM conversion/allowed set,
price tolerance and currency, package multiples — each fires its own
coded finding. Rounding/currency/effective-date semantics are asserted,
not assumed."""

from datetime import date
from decimal import Decimal

import pytest

from soa_db.catalog_line_validation import (
    LinePolicy,
    LinePolicyError,
    MaterialFacts,
    OrderLine,
    validate_lines,
)

WIDGET = MaterialFacts(
    source_id="WID-100",
    display_name="Widget 100 (steel)",
    aliases=("WIDGET 100",),
    attributes={
        "customer_item_ids": ["ACME-77"],
        "base_uom": "EA",
        "uom_conversions": {"BOX": "12", "PAL": "720"},
        "price": "4.50",
        "currency": "EUR",
        "package_multiple": "12",
    },
)
CATALOG = [
    WIDGET,
    MaterialFacts(
        source_id="GAD-205",
        display_name="Gadget 205",
        attributes={"customer_item_ids": ["ACME-88"], "base_uom": "EA", "price": "9.00"},
    ),
]


def line(**overrides: object) -> OrderLine:
    base: dict[str, object] = {
        "row_index": 0,
        "sku": "WID-100",
        "quantity": "24",
        "uom": "EA",
        "unit_price": "4.50",
        "currency": "EUR",
    }
    base.update(overrides)
    return OrderLine(**base)  # type: ignore[arg-type]


class TestCleanLines:
    def test_a_fully_consistent_line_has_no_findings(self) -> None:
        result = validate_lines([(line(), WIDGET)], catalog=CATALOG, as_of=date(2026, 7, 13))
        assert result.findings == ()
        (derived,) = result.derived
        assert derived.base_uom == "EA"
        assert derived.base_quantity == Decimal("24")
        assert derived.base_unit_price == Decimal("4.50")

    def test_box_quantities_convert_exactly_to_the_base_uom(self) -> None:
        # 3 boxes of 12 at 54.00/box = 36 EA at exactly 4.50/EA.
        result = validate_lines([(line(quantity="3", uom="BOX", unit_price="54.00"), WIDGET)])
        assert result.findings == ()
        (derived,) = result.derived
        assert derived.base_quantity == Decimal("36")
        assert derived.base_unit_price == Decimal("4.50")

    def test_customer_item_ids_verify_the_mapping(self) -> None:
        result = validate_lines([(line(sku="acme 077"), WIDGET)])
        assert result.findings == ()
        assert any("customer item id" in note for note in result.notes)

    def test_an_unresolved_material_is_a_warning_not_a_crash(self) -> None:
        result = validate_lines([(line(sku="UNKNOWN-1"), None)])
        (finding,) = result.warnings
        assert finding.code == "material_unresolved"
        assert finding.row_index == 0


class TestItemMapping:
    def test_a_sku_claimed_by_another_record_is_an_error(self) -> None:
        result = validate_lines([(line(sku="ACME-88"), WIDGET)], catalog=CATALOG)
        (finding,) = result.errors
        assert finding.code == "customer_item_maps_elsewhere"
        assert "GAD-205" in finding.message

    def test_a_fuzzy_pick_without_an_identifier_is_a_warning(self) -> None:
        result = validate_lines([(line(sku="steel widget"), WIDGET)], catalog=CATALOG)
        (finding,) = result.warnings
        assert finding.code == "customer_item_mapping_unverified"

    def test_without_the_catalog_the_maps_elsewhere_check_is_skipped_with_a_note(
        self,
    ) -> None:
        result = validate_lines([(line(sku="ACME-88"), WIDGET)])
        assert [f.code for f in result.findings] == ["customer_item_mapping_unverified"]
        assert any("skipped" in note for note in result.notes)


class TestEffectiveDates:
    def test_an_out_of_effect_material_is_an_error(self) -> None:
        retired = MaterialFacts(
            source_id="OLD-1",
            display_name="Legacy Widget",
            effective_to=date(2025, 12, 31),
            attributes={"customer_item_ids": ["OLD-1"]},
        )
        result = validate_lines([(line(sku="OLD-1"), retired)], as_of=date(2026, 7, 13))
        assert [f.code for f in result.errors] == ["material_out_of_effect"]

    def test_no_order_date_means_no_enforcement_and_says_so(self) -> None:
        retired = MaterialFacts(
            source_id="OLD-1", display_name="Legacy", effective_to=date(2020, 1, 1)
        )
        result = validate_lines([(line(sku="OLD-1"), retired)])
        assert result.errors == ()
        assert any("were not enforced" in note for note in result.notes)


class TestUom:
    def test_a_uom_outside_the_allowed_set_is_an_error(self) -> None:
        result = validate_lines([(line(uom="KG"), WIDGET)])
        (finding,) = result.errors
        assert finding.code == "uom_not_allowed"
        assert "BOX" in finding.message and "EA" in finding.message and "PAL" in finding.message

    def test_a_missing_uom_assumes_the_base_with_a_note(self) -> None:
        result = validate_lines([(line(uom=None), WIDGET)])
        assert result.errors == ()
        assert any("'EA' was assumed" in note for note in result.notes)

    def test_a_record_without_uom_facts_limits_the_checks_honestly(self) -> None:
        bare = MaterialFacts(
            source_id="BARE-1", display_name="No UOM facts", attributes={"price": "1.00"}
        )
        result = validate_lines([(line(sku="BARE-1", uom="EA"), bare)])
        assert result.errors == ()
        assert any("checks were limited" in note for note in result.notes)

    def test_malformed_conversions_are_ignored_with_a_note(self) -> None:
        broken = MaterialFacts(
            source_id="BRK-1",
            display_name="Broken factors",
            attributes={"base_uom": "EA", "uom_conversions": {"BOX": "a dozen"}},
        )
        result = validate_lines([(line(sku="BRK-1", uom="BOX"), broken)])
        assert [f.code for f in result.errors] == ["uom_not_allowed"]
        assert any("not a positive decimal" in note for note in result.notes)

    def test_non_numeric_quantities_are_flagged(self) -> None:
        result = validate_lines([(line(quantity="a few"), WIDGET)])
        assert "quantity_not_numeric" in [f.code for f in result.warnings]


class TestPrice:
    def test_within_tolerance_passes_with_a_note(self) -> None:
        # 4.53 vs 4.50 is ~0.67% — inside the default ±1%.
        result = validate_lines([(line(unit_price="4.53"), WIDGET)])
        assert result.findings == ()
        assert any("within tolerance" in note for note in result.notes)

    def test_out_of_tolerance_states_the_band_explicitly(self) -> None:
        result = validate_lines([(line(unit_price="4.60"), WIDGET)])
        (finding,) = result.errors
        assert finding.code == "price_out_of_tolerance"
        # The band is the catalog price ±1%, quantized to 4 places.
        assert "[4.4550, 4.5450]" in finding.message
        assert finding.field_key == "lines.unit_price"

    def test_the_tolerance_is_policy_configurable(self) -> None:
        loose = LinePolicy(price_tolerance_ratio=Decimal("0.05"))
        result = validate_lines([(line(unit_price="4.60"), WIDGET)], policy=loose)
        assert result.errors == ()
        with pytest.raises(LinePolicyError, match="negative"):
            LinePolicy(price_tolerance_ratio=Decimal("-0.1"))

    def test_price_comparison_happens_in_the_base_uom(self) -> None:
        # 1 box costs 60.00 -> 5.00/EA vs catalog 4.50: ~11% off.
        result = validate_lines([(line(quantity="1", uom="BOX", unit_price="60.00"), WIDGET)])
        assert [f.code for f in result.errors] == ["price_out_of_tolerance"]

    def test_a_currency_mismatch_never_invents_an_exchange_rate(self) -> None:
        result = validate_lines([(line(currency="USD", unit_price="4.50"), WIDGET)])
        (finding,) = result.errors
        assert finding.code == "price_currency_mismatch"
        assert "no exchange rate" in finding.message

    def test_a_record_without_a_price_is_noted_not_guessed(self) -> None:
        unpriced = MaterialFacts(
            source_id="NP-1", display_name="Unpriced", attributes={"base_uom": "EA"}
        )
        result = validate_lines([(line(sku="NP-1"), unpriced)])
        assert result.errors == ()
        assert any("carries no price" in note for note in result.notes)


class TestPackageMultiple:
    def test_a_partial_package_is_an_error_with_the_remainder(self) -> None:
        result = validate_lines([(line(quantity="30"), WIDGET)])
        (finding,) = result.errors
        assert finding.code == "package_multiple_violation"
        assert "remainder 6" in finding.message

    def test_the_multiple_applies_to_the_converted_base_quantity(self) -> None:
        # 2 boxes = 24 EA: a whole multiple of 12 even though 2 % 12 != 0.
        result = validate_lines([(line(quantity="2", uom="BOX", unit_price="54.00"), WIDGET)])
        assert result.errors == ()


class TestMatrixAndShape:
    def test_multiple_lines_keep_their_row_indexes(self) -> None:
        lines = [
            (line(row_index=0), WIDGET),
            (line(row_index=1, quantity="30"), WIDGET),
            (line(row_index=2, sku="UNKNOWN"), None),
        ]
        result = validate_lines(lines, catalog=CATALOG, as_of=date(2026, 7, 13))
        by_row = {f.row_index: f.code for f in result.findings}
        assert by_row == {1: "package_multiple_violation", 2: "material_unresolved"}
        assert [d.row_index for d in result.derived] == [0, 1, 2]

    def test_findings_serialize_as_route_reasons_with_the_row(self) -> None:
        result = validate_lines([(line(quantity="30"), WIDGET)])
        reason = result.errors[0].to_reason()
        assert reason["rule_key"] == "catalog.package_multiple_violation"
        assert reason["row_index"] == 0
        assert reason["field_key"] == "lines.quantity"
