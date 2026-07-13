"""Catalog model tests (CAT-001): record validation, the activate
lifecycle (immutable once active, single active version, pointer
moves), and stream bindings (pinned requires an activated version;
rolling is explicit)."""

import uuid
from datetime import date
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.catalogs import (
    Catalog,
    CatalogBindingMode,
    CatalogError,
    CatalogRecordRepository,
    CatalogVersion,
    CatalogVersionRepository,
    activate_catalog_version,
    add_catalog_record,
    bind_catalog_to_stream,
    create_catalog,
    create_catalog_version,
    resolve_catalog_version,
)
from soa_db.repository import OrganizationContext
from soa_db.versioning import ImmutableVersionError, InvalidVersionStateError

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/catalogs.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_catalog_with_draft(db: DatabaseSessions) -> tuple[uuid.UUID, uuid.UUID]:
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
        draft = await create_catalog_version(session, CONTEXT, catalog=catalog, actor_id="user:u-1")
        return catalog.id, draft.id


async def add_widget(db: DatabaseSessions, version_id: uuid.UUID, source_id: str = "SKU-9") -> None:
    async with db.session_scope() as session:
        version = await CatalogVersionRepository(session, CONTEXT).get(version_id)
        assert version is not None
        await add_catalog_record(
            session,
            CONTEXT,
            version=version,
            source_id=source_id,
            display_name="Widget 9mm",
            attributes={"uom": "EA", "price": "12.50"},
            aliases=["WIDGET-9", "Widget Nine"],
            effective_from=date(2026, 1, 1),
        )


class TestCatalogAndRecords:
    async def test_types_and_sources_are_closed_sets(self, db: DatabaseSessions) -> None:
        async with db.session_scope() as session:
            with pytest.raises(CatalogError, match="catalog_type"):
                await create_catalog(
                    session,
                    CONTEXT,
                    name="x",
                    slug="x",
                    catalog_type="spells",
                    source="csv_import",
                    actor_id="user:u-1",
                )

    async def test_records_validate_and_deduplicate_source_ids(self, db: DatabaseSessions) -> None:
        _, draft_id = await make_catalog_with_draft(db)
        await add_widget(db, draft_id)
        async with db.session_scope() as session:
            version = await CatalogVersionRepository(session, CONTEXT).get(draft_id)
            assert version is not None
            with pytest.raises(CatalogError, match="already in this catalog version"):
                await add_catalog_record(
                    session,
                    CONTEXT,
                    version=version,
                    source_id="SKU-9",
                    display_name="Duplicate",
                )
            with pytest.raises(CatalogError, match="effective_to"):
                await add_catalog_record(
                    session,
                    CONTEXT,
                    version=version,
                    source_id="SKU-10",
                    display_name="Backwards dates",
                    effective_from=date(2026, 6, 1),
                    effective_to=date(2026, 1, 1),
                )
        async with db.session_scope() as session:
            version = await CatalogVersionRepository(session, CONTEXT).get(draft_id)
            assert version is not None and version.record_count == 1


class TestActivation:
    async def test_empty_versions_cannot_activate(self, db: DatabaseSessions) -> None:
        catalog_id, draft_id = await make_catalog_with_draft(db)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            version = await session.get(CatalogVersion, draft_id)
            assert catalog is not None and version is not None
            with pytest.raises(CatalogError, match="empty"):
                await activate_catalog_version(
                    session, CONTEXT, catalog=catalog, version=version, actor_id="user:u-1"
                )

    async def test_activation_freezes_supersedes_and_moves_the_pointer(
        self, db: DatabaseSessions
    ) -> None:
        catalog_id, first_id = await make_catalog_with_draft(db)
        await add_widget(db, first_id)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            version = await session.get(CatalogVersion, first_id)
            assert catalog is not None and version is not None
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=version, actor_id="user:u-1"
            )
            assert catalog.active_version_id == first_id
            # Activated versions accept no more records.
            with pytest.raises(InvalidVersionStateError):
                await add_catalog_record(
                    session, CONTEXT, version=version, source_id="SKU-2", display_name="Late"
                )
        # ...and their rows are immutable at the flush guard.
        with pytest.raises(ImmutableVersionError):
            async with db.session_scope() as session:
                version = await session.get(CatalogVersion, first_id)
                assert version is not None
                version.change_summary = "history rewritten"
                await session.flush()
        # A second activation supersedes the first.
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            second = await create_catalog_version(
                session, CONTEXT, catalog=catalog, actor_id="user:u-1"
            )
            await add_catalog_record(
                session, CONTEXT, version=second, source_id="SKU-9", display_name="Widget v2"
            )
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=second, actor_id="user:u-1"
            )
            second_id = second.id
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            first = await session.get(CatalogVersion, first_id)
            assert catalog is not None and first is not None
            assert catalog.active_version_id == second_id
            assert first.state == "superseded"


class TestBindings:
    async def make_active(self, db: DatabaseSessions) -> tuple[uuid.UUID, uuid.UUID]:
        catalog_id, draft_id = await make_catalog_with_draft(db)
        await add_widget(db, draft_id)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            version = await session.get(CatalogVersion, draft_id)
            assert catalog is not None and version is not None
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=version, actor_id="user:u-1"
            )
        return catalog_id, draft_id

    async def test_pinned_bindings_require_an_activated_version(self, db: DatabaseSessions) -> None:
        catalog_id, draft_id = await make_catalog_with_draft(db)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            with pytest.raises(CatalogError, match="name the version"):
                await bind_catalog_to_stream(
                    session,
                    CONTEXT,
                    stream_id=STREAM,
                    catalog=catalog,
                    mode=CatalogBindingMode.PINNED,
                    actor_id="user:u-1",
                )
            with pytest.raises(CatalogError, match="activate it first"):
                await bind_catalog_to_stream(
                    session,
                    CONTEXT,
                    stream_id=STREAM,
                    catalog=catalog,
                    mode=CatalogBindingMode.PINNED,
                    pinned_version_id=draft_id,
                    actor_id="user:u-1",
                )

    async def test_rolling_bindings_refuse_a_pin_and_follow_the_active_version(
        self, db: DatabaseSessions
    ) -> None:
        catalog_id, version_id = await self.make_active(db)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            with pytest.raises(CatalogError, match="rolling"):
                await bind_catalog_to_stream(
                    session,
                    CONTEXT,
                    stream_id=STREAM,
                    catalog=catalog,
                    mode=CatalogBindingMode.ROLLING,
                    pinned_version_id=version_id,
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
            resolved = await resolve_catalog_version(session, CONTEXT, binding=binding)
            assert resolved is not None and resolved.id == version_id

    async def test_pinned_bindings_stay_on_their_version_across_new_activations(
        self, db: DatabaseSessions
    ) -> None:
        catalog_id, first_id = await self.make_active(db)
        async with db.session_scope() as session:
            catalog = await session.get(Catalog, catalog_id)
            assert catalog is not None
            binding = await bind_catalog_to_stream(
                session,
                CONTEXT,
                stream_id=STREAM,
                catalog=catalog,
                mode=CatalogBindingMode.PINNED,
                pinned_version_id=first_id,
                actor_id="user:u-1",
            )
            second = await create_catalog_version(
                session, CONTEXT, catalog=catalog, actor_id="user:u-1"
            )
            await add_catalog_record(
                session, CONTEXT, version=second, source_id="SKU-9", display_name="Widget v2"
            )
            await activate_catalog_version(
                session, CONTEXT, catalog=catalog, version=second, actor_id="user:u-1"
            )
            resolved = await resolve_catalog_version(session, CONTEXT, binding=binding)
            assert resolved is not None and resolved.id == first_id  # still pinned

    async def test_records_carry_aliases_attributes_and_effective_dates(
        self, db: DatabaseSessions
    ) -> None:
        _, version_id = await self.make_active(db)
        async with db.session_scope() as session:
            records = await CatalogRecordRepository(session, CONTEXT).list_for_version(version_id)
            (record,) = records
            assert record.aliases == ["WIDGET-9", "Widget Nine"]
            assert record.attributes["price"] == "12.50"
            assert record.effective_from == date(2026, 1, 1)
