"""Catalog matching tests (CAT-006): the identifier/alias/effective-date
matrix, deterministic tiers and reasons, surfaced ambiguity, and the
binding-scoped index."""

import uuid
from datetime import date
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.catalog_matching import (
    CatalogMatchIndex,
    RecordFacts,
    build_match_index,
    normalize_identifier,
    normalize_text,
)
from soa_db.catalogs import (
    CatalogBindingMode,
    activate_catalog_version,
    add_catalog_record,
    bind_catalog_to_stream,
    create_catalog,
    create_catalog_version,
)
from soa_db.repository import OrganizationContext

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONTEXT = OrganizationContext(organization_id=ORG)


def rec(
    source_id: str,
    name: str,
    aliases: tuple[str, ...] = (),
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> RecordFacts:
    return RecordFacts(
        record_id=uuid.uuid5(uuid.NAMESPACE_URL, source_id),
        source_id=source_id,
        display_name=name,
        aliases=aliases,
        effective_from=effective_from,
        effective_to=effective_to,
    )


RECORDS = [
    rec("SKU-9", "Widget 9mm", ("WIDGET-9", "Widget Nine")),
    rec("SKU-10", "Flange Kit", ("FLANGE,KIT",)),
    rec("CUST-001", "Acme GmbH", ("ACME",)),
]


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("sku-0009", "SKU9"),
            ("SKU 9", "SKU9"),
            ("sku_9", "SKU9"),
            ("SKU/0010", "SKU10"),
            ("cust.001", "CUST1"),
        ],
    )
    def test_identifier_normalization(self, raw: str, expected: str) -> None:
        assert normalize_identifier(raw) == expected

    def test_text_normalization(self) -> None:
        assert normalize_text("  Widget,   9MM! ") == "widget 9mm"


class TestTierMatrix:
    def index(self) -> CatalogMatchIndex:
        return CatalogMatchIndex(list(RECORDS))

    def test_exact_identifier_wins_first(self) -> None:
        result = self.index().match("SKU-9")
        assert result.tier == "exact_identifier"
        (candidate,) = result.candidates
        assert candidate.source_id == "SKU-9"
        assert "exactly" in candidate.reason

    def test_normalized_identifier_finds_sloppy_ids(self) -> None:
        result = self.index().match("sku 0009")
        assert result.tier == "normalized_identifier"
        (candidate,) = result.candidates
        assert candidate.source_id == "SKU-9"
        assert "'SKU9'" in candidate.reason

    def test_exact_alias_matches_before_normalized_text(self) -> None:
        result = self.index().match("WIDGET-9")
        assert result.tier == "exact_text"
        (candidate,) = result.candidates
        assert candidate.matched_text == "WIDGET-9"
        assert "alias" in candidate.reason

    def test_normalized_text_matches_names_and_aliases(self) -> None:
        result = self.index().match("widget   NINE!")
        assert result.tier == "normalized_text"
        (candidate,) = result.candidates
        assert candidate.source_id == "SKU-9"
        result = self.index().match("flange kit")
        assert result.tier == "normalized_text"
        assert result.candidates[0].source_id == "SKU-10"

    def test_no_match_carries_the_reason(self) -> None:
        result = self.index().match("unobtainium")
        assert result.tier is None
        assert result.candidates == ()
        assert any("no record matches" in note for note in result.notes)

    def test_empty_queries_match_nothing(self) -> None:
        result = self.index().match("   ")
        assert result.candidates == ()

    def test_ambiguity_is_surfaced_deterministically(self) -> None:
        records = [
            *RECORDS,
            rec("SKU-11", "Widget 9mm (bulk)", ("Widget Nine",)),
        ]
        first = CatalogMatchIndex(records).match("widget nine")
        second = CatalogMatchIndex(list(reversed(records))).match("widget nine")
        assert first.tier == "normalized_text"
        assert [c.source_id for c in first.candidates] == ["SKU-11", "SKU-9"]
        assert [c.source_id for c in second.candidates] == ["SKU-11", "SKU-9"]  # order stable
        assert any("ambiguity" in note for note in first.notes)


class TestEffectiveDates:
    def test_out_of_effect_records_are_excluded_with_reasons(self) -> None:
        records = [
            rec("OLD-1", "Legacy Widget", effective_to=date(2025, 12, 31)),
            rec("NEW-1", "Legacy Widget", effective_from=date(2026, 1, 1)),
            rec("FUT-1", "Future Widget", effective_from=date(2027, 1, 1)),
        ]
        index = CatalogMatchIndex(records, as_of=date(2026, 7, 13))
        result = index.match("Legacy Widget")
        assert [c.source_id for c in result.candidates] == ["NEW-1"]
        assert set(result.excluded) == {"OLD-1", "FUT-1"}
        assert any("out of effect on 2026-07-13" in note for note in result.notes)

    def test_records_without_dates_are_always_effective(self) -> None:
        index = CatalogMatchIndex(list(RECORDS), as_of=date(2030, 1, 1))
        assert index.match("SKU-9").candidates != ()

    def test_no_as_of_means_no_filtering(self) -> None:
        records = [rec("OLD-1", "Legacy Widget", effective_to=date(2020, 1, 1))]
        result = CatalogMatchIndex(records).match("Legacy Widget")
        assert [c.source_id for c in result.candidates] == ["OLD-1"]


class TestBindingScopedIndex:
    @pytest.fixture
    async def db(self, tmp_path: Path) -> DatabaseSessions:
        engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/matching.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return DatabaseSessions(engine)

    async def test_the_index_resolves_through_the_stream_binding(
        self, db: DatabaseSessions
    ) -> None:
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
            version = await create_catalog_version(
                session, CONTEXT, catalog=catalog, actor_id="user:u-1"
            )
            await add_catalog_record(
                session,
                CONTEXT,
                version=version,
                source_id="SKU-9",
                display_name="Widget 9mm",
                aliases=["WIDGET-9"],
            )
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=version, actor_id="user:u-1"
            )
            binding = await bind_catalog_to_stream(
                session,
                CONTEXT,
                stream_id=STREAM,
                catalog=catalog,
                mode=CatalogBindingMode.ROLLING,
                actor_id="user:u-1",
            )
            index = await build_match_index(session, CONTEXT, binding=binding)
            assert index is not None
            result = index.match("widget 9mm")
            assert result.candidates[0].source_id == "SKU-9"

    async def test_a_binding_without_a_usable_version_yields_no_index(
        self, db: DatabaseSessions
    ) -> None:
        async with db.session_scope() as session:
            catalog = await create_catalog(
                session,
                CONTEXT,
                name="Empty",
                slug="empty",
                catalog_type="custom",
                source="manual",
                actor_id="user:u-1",
            )
            binding = await bind_catalog_to_stream(
                session,
                CONTEXT,
                stream_id=STREAM,
                catalog=catalog,
                mode=CatalogBindingMode.ROLLING,
                actor_id="user:u-1",
            )
            assert (await build_match_index(session, CONTEXT, binding=binding)) is None
