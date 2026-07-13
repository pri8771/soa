"""CSV import pipeline tests (CAT-002): file variants (delimiters, BOM,
Latin-1, CRLF), malicious cells (formula injection, NUL bytes), per-row
issues, the partial-import guard, the added/changed/deactivated
preview, and the end-to-end draft -> explicit activation flow."""

import uuid
from datetime import date
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.catalog_import import (
    CatalogImportError,
    ColumnMapping,
    apply_import,
    parse_catalog_csv,
    preview_import,
)
from soa_db.catalogs import (
    Catalog,
    CatalogRecordRepository,
    activate_catalog_version,
    create_catalog,
)
from soa_db.repository import OrganizationContext

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)

MAPPING = ColumnMapping(
    source_id="sku",
    display_name="name",
    aliases="aliases",
    effective_from="valid_from",
    attributes={"uom": "unit", "price": "list_price"},
)

GOOD_CSV = (
    b"sku,name,aliases,valid_from,unit,list_price\n"
    b"SKU-1,Widget 9mm,WIDGET-9|Widget Nine,2026-01-01,EA,12.50\n"
    b"SKU-2,Flange Kit,,,BOX,99.00\n"
)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/catalog-import.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_catalog(db: DatabaseSessions) -> uuid.UUID:
    async with db.session_scope() as session:
        catalog = await create_catalog(
            session,
            CONTEXT,
            name="Products",
            slug="products",
            catalog_type="products",
            source="csv_import",
            actor_id="user:u-1",
        )
        return catalog.id


class TestParsingVariants:
    def test_a_clean_utf8_file_parses_fully(self) -> None:
        result = parse_catalog_csv(GOOD_CSV, MAPPING)
        assert result.ok
        assert result.encoding == "utf-8"
        assert result.delimiter == ","
        first, second = result.records
        assert first.source_id == "SKU-1"
        assert first.aliases == ("WIDGET-9", "Widget Nine")
        assert first.attributes == {"uom": "EA", "price": "12.50"}
        assert first.effective_from == date(2026, 1, 1)
        assert second.aliases == ()

    def test_semicolon_and_crlf_files_are_sniffed(self) -> None:
        data = (
            b"sku;name;aliases;valid_from;unit;list_price\r\nSKU-1;Widget;W-9;;EA;1.00\r\n"
        )
        result = parse_catalog_csv(data, MAPPING)
        assert result.ok and result.delimiter == ";"

    def test_utf8_bom_is_stripped(self) -> None:
        result = parse_catalog_csv(b"\xef\xbb\xbf" + GOOD_CSV, MAPPING)
        assert result.ok and result.encoding == "utf-8-sig"
        assert result.records[0].source_id == "SKU-1"

    def test_latin1_fallback_carries_a_warning(self) -> None:
        data = (
            "sku,name,aliases,valid_from,unit,list_price\nSKU-1,Caf\xe9 Filter,,,EA,2.00\n"
        ).encode("latin-1")
        result = parse_catalog_csv(data, MAPPING)
        assert result.ok and result.encoding == "latin-1"
        assert result.records[0].display_name == "Café Filter"
        assert any("Latin-1" in warning for warning in result.warnings)

    def test_missing_mapped_columns_fail_loudly(self) -> None:
        with pytest.raises(CatalogImportError, match="columns the file does not have"):
            parse_catalog_csv(b"id,title\nX,Y\n", MAPPING)


class TestHostileCells:
    def test_formula_like_cells_are_kept_verbatim_but_flagged(self) -> None:
        data = (
            b"sku,name,aliases,valid_from,unit,list_price\n"
            b'SKU-1,"=HYPERLINK(""http://evil"",""click"")",,,EA,1.00\n'
            b"SKU-2,+cmd|calc,,,EA,-5.00\n"
        )
        result = parse_catalog_csv(data, MAPPING)
        assert result.ok
        first, second = result.records
        assert first.display_name.startswith("=HYPERLINK")  # verbatim text, never executed
        assert any("spreadsheet formula" in warning for warning in first.warnings)
        assert any("spreadsheet formula" in warning for warning in second.warnings)
        # A legitimate negative amount is NOT flagged.
        assert not any("list_price" in warning for warning in second.warnings)

    def test_nul_bytes_are_refused(self) -> None:
        with pytest.raises(CatalogImportError, match="NUL"):
            parse_catalog_csv(b"sku,name\x00\nA,B\n", MAPPING)

    def test_the_export_encoder_defuses_formula_cells(self) -> None:
        from soa_canonical.export_encoding import ExportMetadata, encode_csv_export

        metadata = ExportMetadata(
            schema_version="1.0.0",
            mapping_version_number=1,
            integration_slug="erp",
            business_key="export:i:d:r",
            document_id="d",
            run_id="r",
            exported_at="2026-07-13T00:00:00Z",
        )
        payload = {"PoNumber": "=2+5", "Lines": [{"Sku": "@SUM(A1)", "Amount": "-5.00"}]}
        csv_bytes = encode_csv_export(payload, metadata, lines_key="Lines")
        text = csv_bytes.decode("utf-8")
        assert "'=2+5" in text
        assert "'@SUM(A1)" in text
        assert ",-5.00" in text  # negative amounts stay untouched


class TestRowIssues:
    def test_issues_carry_row_numbers_and_never_drop_silently(self) -> None:
        data = (
            b"sku,name,aliases,valid_from,unit,list_price\n"
            b"SKU-1,Widget,,,EA,1.00\n"
            b",Missing id,,,EA,1.00\n"
            b"SKU-3,,,,EA,1.00\n"
            b"SKU-4,Bad date,,01.06.2026,EA,1.00\n"
            b"SKU-1,Duplicate,,,EA,1.00\n"
        )
        result = parse_catalog_csv(data, MAPPING)
        assert len(result.records) == 1
        messages = {issue.row_number: issue.message for issue in result.issues}
        assert "missing 'sku'" in messages[2]
        assert "missing 'name'" in messages[3]
        assert "not an ISO date" in messages[4]
        assert "duplicate source id" in messages[5]


class TestApplyAndPreview:
    async def test_partial_imports_need_the_explicit_flag(self, db: DatabaseSessions) -> None:
        catalog_id = await make_catalog(db)
        data = (
            b"sku,name,aliases,valid_from,unit,list_price\n"
            b"SKU-1,Widget,,,EA,1.00\n"
            b",broken,,,EA,1.00\n"
        )
        parsed = parse_catalog_csv(data, MAPPING)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            with pytest.raises(CatalogImportError, match="allow_partial"):
                await apply_import(
                    session, CONTEXT, catalog=catalog, parsed=parsed, actor_id="user:u-1"
                )
            draft = await apply_import(
                session,
                CONTEXT,
                catalog=catalog,
                parsed=parsed,
                actor_id="user:u-1",
                allow_partial=True,
            )
            assert draft.record_count == 1
            assert draft.state == "draft"  # never active as a side effect

    async def test_end_to_end_import_then_explicit_activation(self, db: DatabaseSessions) -> None:
        catalog_id = await make_catalog(db)
        parsed = parse_catalog_csv(GOOD_CSV, MAPPING)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            draft = await apply_import(
                session, CONTEXT, catalog=catalog, parsed=parsed, actor_id="user:u-1"
            )
            assert catalog.active_version_id is None  # still not live
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=draft, actor_id="user:u-1"
            )
            assert catalog.active_version_id == draft.id
            records = await CatalogRecordRepository(session, CONTEXT).list_for_version(draft.id)
            assert [record.source_id for record in records] == ["SKU-1", "SKU-2"]

    async def test_the_preview_reports_added_changed_deactivated_unchanged(
        self, db: DatabaseSessions
    ) -> None:
        catalog_id = await make_catalog(db)
        parsed = parse_catalog_csv(GOOD_CSV, MAPPING)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            first_preview = await preview_import(session, CONTEXT, catalog=catalog, parsed=parsed)
            assert first_preview.added == ("SKU-1", "SKU-2")  # nothing active yet
            draft = await apply_import(
                session, CONTEXT, catalog=catalog, parsed=parsed, actor_id="user:u-1"
            )
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=draft, actor_id="user:u-1"
            )
        updated = (
            b"sku,name,aliases,valid_from,unit,list_price\n"
            b"SKU-1,Widget 9mm,WIDGET-9|Widget Nine,2026-01-01,EA,12.50\n"  # unchanged
            b"SKU-3,Brand New,,,EA,3.00\n"  # added; SKU-2 disappears
        )
        reparsed = parse_catalog_csv(updated, MAPPING)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            preview = await preview_import(session, CONTEXT, catalog=catalog, parsed=reparsed)
            assert preview.added == ("SKU-3",)
            assert preview.changed == ()
            assert preview.deactivated == ("SKU-2",)
            assert preview.unchanged == 1
        changed_file = GOOD_CSV.replace(b"12.50", b"14.00")
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            preview = await preview_import(
                session, CONTEXT, catalog=catalog, parsed=parse_catalog_csv(changed_file, MAPPING)
            )
            assert preview.changed == ("SKU-1",)
