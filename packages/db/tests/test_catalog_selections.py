"""Durable catalog identity tests: exact ids, value/version staleness,
append-only history, and tenant isolation."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.catalog_selections import (
    CatalogFieldSelectionRepository,
    CatalogSelectionError,
    CatalogSelectionSource,
    CatalogSelectionStatus,
    latest_catalog_selections,
    record_catalog_selection,
    resolve_catalog_identities,
)
from soa_db.catalogs import (
    Catalog,
    CatalogBindingMode,
    CatalogRecord,
    CatalogVersion,
    activate_catalog_version,
    add_catalog_record,
    bind_catalog_to_stream,
    create_catalog,
    create_catalog_version,
)
from soa_db.data_export import collect_document_export
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONTEXT = OrganizationContext(organization_id=ORG)
OTHER_CONTEXT = OrganizationContext(organization_id=OTHER_ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/catalog-selections.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _seed_catalog(
    session: AsyncSession,
) -> tuple[Catalog, CatalogVersion, CatalogRecord]:
    catalog = await create_catalog(
        session,
        CONTEXT,
        name="Products",
        slug="products",
        catalog_type="products",
        source="manual",
        actor_id="user:test",
    )
    version = await create_catalog_version(session, CONTEXT, catalog=catalog, actor_id="user:test")
    record = await add_catalog_record(
        session,
        CONTEXT,
        version=version,
        source_id="WID-100",
        display_name="Widget 100",
    )
    await activate_catalog_version(
        session, CONTEXT, catalog=catalog, version=version, actor_id="user:test"
    )
    await bind_catalog_to_stream(
        session,
        CONTEXT,
        stream_id=STREAM,
        catalog=catalog,
        mode=CatalogBindingMode.ROLLING,
        actor_id="user:test",
    )
    return catalog, version, record


async def test_exact_identity_is_retained_resolved_and_tenant_scoped(
    db: DatabaseSessions,
) -> None:
    document_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with db.session_scope() as session:
        catalog, version, record = await _seed_catalog(session)
        selection = await record_catalog_selection(
            session,
            CONTEXT,
            document_id=document_id,
            run_id=run_id,
            task_id=None,
            field_key="lines.sku",
            row_index=0,
            status=CatalogSelectionStatus.SELECTED,
            selection_source=CatalogSelectionSource.MACHINE,
            catalog=catalog,
            version=version,
            record=record,
            matched_value="Widget 100",
            selected_by="worker",
        )
        assert selection.catalog_record_id == record.id
        assert selection.catalog_version_id == version.id
        resolved = await resolve_catalog_identities(
            session,
            CONTEXT,
            stream_id=STREAM,
            run_id=run_id,
            values={("lines.sku", 0): "Widget 100"},
            as_of=version.created_at.date(),
        )
        assert resolved.issues == ()
        assert resolved.identities[("lines.sku", 0)].source_id == "WID-100"

        # Repository scoping is enforced even on SQLite, beneath API auth.
        assert (
            await CatalogFieldSelectionRepository(session, OTHER_CONTEXT).list_for_run(run_id) == []
        )


async def test_latest_event_invalidates_old_identity_and_detects_value_change(
    db: DatabaseSessions,
) -> None:
    document_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with db.session_scope() as session:
        catalog, version, record = await _seed_catalog(session)
        await record_catalog_selection(
            session,
            CONTEXT,
            document_id=document_id,
            run_id=run_id,
            task_id=None,
            field_key="lines.sku",
            row_index=0,
            status=CatalogSelectionStatus.SELECTED,
            selection_source=CatalogSelectionSource.MACHINE,
            catalog=catalog,
            version=version,
            record=record,
            matched_value="Widget 100",
            selected_by="worker",
        )
        await record_catalog_selection(
            session,
            CONTEXT,
            document_id=document_id,
            run_id=run_id,
            task_id=uuid.uuid4(),
            field_key="lines.sku",
            row_index=0,
            status=CatalogSelectionStatus.NEEDS_REVIEW,
            selection_source=CatalogSelectionSource.CORRECTION,
            catalog=catalog,
            version=version,
            record=None,
            matched_value="unknown item",
            selected_by="user:reviewer",
        )
        rows = await CatalogFieldSelectionRepository(session, CONTEXT).list_for_run(run_id)
        assert latest_catalog_selections(rows)[("lines.sku", 0)].status == "needs_review"
        resolved = await resolve_catalog_identities(
            session,
            CONTEXT,
            stream_id=STREAM,
            run_id=run_id,
            values={("lines.sku", 0): "changed again"},
            as_of=version.created_at.date(),
        )
        assert [issue.code for issue in resolved.issues] == ["value_changed"]


async def test_rolling_version_change_and_cross_tenant_objects_fail_closed(
    db: DatabaseSessions,
) -> None:
    document_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with db.session_scope() as session:
        catalog, version, record = await _seed_catalog(session)
        await record_catalog_selection(
            session,
            CONTEXT,
            document_id=document_id,
            run_id=run_id,
            task_id=None,
            field_key="lines.sku",
            row_index=0,
            status=CatalogSelectionStatus.SELECTED,
            selection_source=CatalogSelectionSource.MACHINE,
            catalog=catalog,
            version=version,
            record=record,
            matched_value="WID-100",
            selected_by="worker",
        )
        replacement = await create_catalog_version(
            session, CONTEXT, catalog=catalog, actor_id="user:test"
        )
        await add_catalog_record(
            session,
            CONTEXT,
            version=replacement,
            source_id="WID-100",
            display_name="Widget 100 rev B",
        )
        await activate_catalog_version(
            session,
            CONTEXT,
            catalog=catalog,
            version=replacement,
            actor_id="user:test",
        )
        resolved = await resolve_catalog_identities(
            session,
            CONTEXT,
            stream_id=STREAM,
            run_id=run_id,
            values={("lines.sku", 0): "WID-100"},
            as_of=replacement.created_at.date(),
        )
        assert [issue.code for issue in resolved.issues] == ["version_changed"]

        with pytest.raises(CatalogSelectionError, match="different organization"):
            await record_catalog_selection(
                session,
                OTHER_CONTEXT,
                document_id=document_id,
                run_id=run_id,
                task_id=None,
                field_key="lines.sku",
                row_index=0,
                status=CatalogSelectionStatus.SELECTED,
                selection_source=CatalogSelectionSource.MACHINE,
                catalog=catalog,
                version=replacement,
                record=record,
                matched_value="WID-100",
                selected_by="worker",
            )


async def test_document_export_includes_identity_but_respects_snapshot_boundary(
    db: DatabaseSessions,
) -> None:
    document_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with db.session_scope() as session:
        catalog, version, record = await _seed_catalog(session)
        snapshot = utcnow()
        await record_catalog_selection(
            session,
            CONTEXT,
            document_id=document_id,
            run_id=run_id,
            task_id=None,
            field_key="lines.sku",
            row_index=0,
            status=CatalogSelectionStatus.SELECTED,
            selection_source=CatalogSelectionSource.MACHINE,
            catalog=catalog,
            version=version,
            record=record,
            matched_value="WID-100",
            selected_by="worker",
        )
        at_snapshot = await collect_document_export(
            session,
            organization_id=ORG,
            document_id=document_id,
            snapshot_at=snapshot,
        )
        current = await collect_document_export(
            session, organization_id=ORG, document_id=document_id
        )
        assert at_snapshot.records["catalog_field_selections"] == []
        assert current.records["catalog_field_selections"][0]["source_id"] == "WID-100"
