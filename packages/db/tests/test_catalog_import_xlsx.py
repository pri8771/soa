"""XLSX import tests (CAT-003): safe workbook reading (macros inert,
formulas never evaluated — cached values only), sheet selection, cell
coercion, and the shared validation/preview semantics."""

import io
import zipfile
from datetime import date, datetime

import pytest
from openpyxl import Workbook

from soa_db.catalog_import import CatalogImportError, ColumnMapping
from soa_db.catalog_import_xlsx import parse_catalog_xlsx

MAPPING = ColumnMapping(
    source_id="sku",
    display_name="name",
    aliases="aliases",
    effective_from="valid_from",
    attributes={"price": "list_price"},
)


def workbook_bytes(rows: list[list[object]], *, sheet_title: str = "Sheet1") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = sheet_title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


HEADER = ["sku", "name", "aliases", "valid_from", "list_price"]


class TestReading:
    def test_a_clean_workbook_parses_with_coerced_cells(self) -> None:
        data = workbook_bytes(
            [
                HEADER,
                ["SKU-1", "Widget 9mm", "WIDGET-9|Widget Nine", date(2026, 1, 1), 12.5],
                ["SKU-2", "Flange Kit", None, None, 99],
            ]
        )
        result = parse_catalog_xlsx(data, MAPPING)
        assert result.ok
        assert result.encoding == "xlsx"
        first, second = result.records
        assert first.effective_from == date(2026, 1, 1)  # date cell -> ISO -> date
        assert first.attributes["price"] == "12.5"
        assert second.attributes["price"] == "99"  # integral float loses the .0
        assert second.aliases == ()

    def test_datetime_cells_at_midnight_become_dates(self) -> None:
        data = workbook_bytes([HEADER, ["SKU-1", "Widget", None, datetime(2026, 1, 1, 0, 0), 1]])
        result = parse_catalog_xlsx(data, MAPPING)
        assert result.ok and result.records[0].effective_from == date(2026, 1, 1)

    def test_fully_empty_rows_are_skipped_not_errors(self) -> None:
        data = workbook_bytes(
            [HEADER, [None, None, None, None, None], ["SKU-1", "Widget", None, None, 1]]
        )
        result = parse_catalog_xlsx(data, MAPPING)
        assert result.ok and len(result.records) == 1

    def test_garbage_bytes_fail_loudly(self) -> None:
        with pytest.raises(CatalogImportError, match="could not be read"):
            parse_catalog_xlsx(b"not a zip at all", MAPPING)


class TestSheetSelection:
    def test_a_named_sheet_is_used_and_unknown_names_list_the_sheets(self) -> None:
        workbook = Workbook()
        first = workbook.active
        assert first is not None
        first.title = "Ignore me"
        first.append(["junk"])
        products = workbook.create_sheet("Products")
        products.append(HEADER)
        products.append(["SKU-1", "Widget", None, None, 1])
        buffer = io.BytesIO()
        workbook.save(buffer)
        data = buffer.getvalue()

        result = parse_catalog_xlsx(data, MAPPING, sheet="Products")
        assert result.ok and result.records[0].source_id == "SKU-1"
        with pytest.raises(CatalogImportError, match=r"Ignore me.*Products"):
            parse_catalog_xlsx(data, MAPPING, sheet="Prices")


class TestHostileWorkbooks:
    def test_macro_carrying_workbooks_are_read_as_inert_data(self) -> None:
        """A vbaProject payload travels in the zip; nothing executes —
        openpyxl has no VBA engine and we open without keep_vba."""
        base = workbook_bytes([HEADER, ["SKU-1", "Widget", None, None, 1]])
        with_macro = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(base)) as source,
            zipfile.ZipFile(with_macro, "w") as target,
        ):
            for item in source.infolist():
                target.writestr(item, source.read(item.filename))
            target.writestr("xl/vbaProject.bin", b"\xd0\xcf\x11\xe0 fake macro blob")
        result = parse_catalog_xlsx(with_macro.getvalue(), MAPPING)
        assert result.ok
        assert result.records[0].source_id == "SKU-1"

    def test_formula_cells_yield_cached_values_never_evaluation(self) -> None:
        """openpyxl with data_only=True returns the CACHED result of a
        formula. A workbook written fresh has no cache, so the cell is
        empty — proving nothing on our side computes it."""
        data = workbook_bytes([HEADER, ["SKU-1", '=HYPERLINK("http://evil")', None, None, "=1+1"]])
        result = parse_catalog_xlsx(data, MAPPING)
        # No cached value exists -> the name cell is EMPTY, reported as
        # a missing-name row issue rather than an evaluated formula.
        assert not result.ok
        assert any("missing 'name'" in issue.message for issue in result.issues)

    def test_formula_looking_strings_get_the_shared_warning(self) -> None:
        # "@..." is text to openpyxl (only leading "=" makes a formula),
        # but it WOULD execute as an implicit-intersection formula when
        # re-opened in Excel — exactly what the shared warning covers.
        data = workbook_bytes([HEADER, ["SKU-1", "@SUM(A1)", None, None, 1]])
        result = parse_catalog_xlsx(data, MAPPING)
        assert result.ok
        (record,) = result.records
        assert record.display_name == "@SUM(A1)"  # verbatim text
        assert any("spreadsheet formula" in warning for warning in record.warnings)
