import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table

from soa_db import GUID, Base, DatabaseSessions, create_database_engine
from soa_db.data_export_jobs import DataExportJobRepository, new_data_export_job
from soa_db.documents import SourceChannel, create_document
from soa_db.organization_export import ORGANIZATION_EXPORT_CATEGORIES
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow
from soa_storage import MemoryObjectStore
from soa_worker.data_export_orchestrator import (
    _put_json_retry_safe,
    cleanup_expired_data_export,
    delete_export_objects,
    execute_data_export_batch,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG)

CONTROL_METADATA = MetaData()
ORGANIZATIONS = Table(
    "organizations",
    CONTROL_METADATA,
    Column("id", GUID(), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
WORKSPACES = Table(
    "workspaces",
    CONTROL_METADATA,
    Column("id", GUID(), primary_key=True),
    Column("organization_id", GUID(), nullable=False),
    Column("name", String(200), nullable=False),
    Column("configuration", JSON(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
USERS = Table(
    "users",
    CONTROL_METADATA,
    Column("id", GUID(), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
MEMBERSHIPS = Table(
    "memberships",
    CONTROL_METADATA,
    Column("id", GUID(), primary_key=True),
    Column("organization_id", GUID(), nullable=False),
    Column("user_id", GUID(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# The worker unit test creates a deliberately minimal reflection-compatible
# version of API-owned tables. Shared-db models already present in Base keep
# their real schema. This proves the worker has no soa_api import dependency
# while the production collector still fails closed when a migrated table is
# actually absent.
for export_category in ORGANIZATION_EXPORT_CATEGORIES:
    if (
        export_category.table_name in CONTROL_METADATA.tables
        or export_category.table_name in Base.metadata.tables
    ):
        continue
    Table(
        export_category.table_name,
        CONTROL_METADATA,
        Column("id", GUID(), primary_key=True),
        Column("organization_id", GUID(), nullable=False),
        Column(export_category.snapshot_column, DateTime(timezone=True), nullable=False),
    )


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/large-export.db")
    async with engine.begin() as conn:
        await conn.run_sync(CONTROL_METADATA.create_all)
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_organization_export_builds_bounded_parts_and_manifest(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        before_snapshot = utcnow() - timedelta(seconds=1)
        await session.execute(
            ORGANIZATIONS.insert(),
            {"id": ORG, "name": "Customer", "created_at": before_snapshot},
        )
        await session.execute(
            WORKSPACES.insert(),
            {
                "id": uuid.uuid4(),
                "organization_id": ORG,
                "name": "Main",
                "configuration": {
                    "client_secret": "must-not-leak",
                    "secret_reference": "vault://customer/workspace",
                },
                "created_at": before_snapshot,
            },
        )
        for index in range(2):
            await create_document(
                session,
                CONTEXT,
                stream_id=STREAM,
                source_channel=SourceChannel.UPLOAD,
                original_filename=f"po-{index}.pdf",
                content_sha256=f"{index + 1:064x}",
                size_bytes=100,
                content_type="application/pdf",
                actor_id="user:test",
            )
        job = new_data_export_job(
            organization_id=ORG,
            scope="organization",
            created_by="user:test",
        )
        session.add(job)
        await session.flush()
        export_id = job.id

    # A document created after the immutable export snapshot is deliberately
    # excluded even if it exists before the worker claims the job.
    async with db.session_scope() as session:
        await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="after-snapshot.pdf",
            content_sha256="f" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )

    store = MemoryObjectStore()
    result = None
    for _ in range(len(ORGANIZATION_EXPORT_CATEGORIES) + 5):
        async with db.session_scope() as session:
            result = await execute_data_export_batch(session, store, CONTEXT, export_id=export_id)
        if result.complete:
            break
    assert result is not None and result.complete is True and result.processed == 2

    async with db.session_scope() as session:
        stored = await DataExportJobRepository(session, CONTEXT).get(export_id)
        assert stored is not None and stored.state == "succeeded"
        assert stored.processed_documents == stored.total_documents == 2
        control_parts = [
            part
            for part in stored.parts
            if isinstance(part, dict) and part.get("part_type") == "organization_records"
        ]
        document_parts = [
            part
            for part in stored.parts
            if isinstance(part, dict) and part.get("part_type") == "document_records"
        ]
        assert len(control_parts) == len(ORGANIZATION_EXPORT_CATEGORIES)
        assert len(document_parts) == 2
        assert all(part["complete"] is True for part in control_parts)
        workspace_part = next(part for part in control_parts if part["category"] == "workspaces")
        workspace_payload = await store.get(str(workspace_part["object_key"]))
        assert b"must-not-leak" not in workspace_payload
        assert b"[redacted]" in workspace_payload
        assert b"vault://customer/workspace" in workspace_payload
        assert stored.manifest_object_key is not None
        manifest = await store.get(stored.manifest_object_key)
        assert b'"total_documents": 2' in manifest
        assert b'"categories": [' in manifest
        assert b'"organization"' in manifest
        assert b'"document_bundle"' in manifest
        assert b'"object_key"' not in manifest

        stored.expires_at = utcnow() - timedelta(seconds=1)

    async with db.session_scope() as session:
        deleted = await cleanup_expired_data_export(
            session,
            store,
            CONTEXT,
            export_id=export_id,
        )
        assert deleted == 5

    async with db.session_scope() as session:
        expired = await DataExportJobRepository(session, CONTEXT).get(export_id)
        assert expired is not None
        assert expired.state == "expired"
        assert expired.parts == []
        assert expired.manifest_object_key is None
        assert await store.list_keys(f"data-exports/{ORG}/{export_id}/") == []


async def test_export_part_retry_accepts_identical_bytes_and_rejects_replacement() -> None:
    store = MemoryObjectStore()
    key = "data-exports/org/export/organization/roles/part-000001.json"
    original = b'{"records":[]}'
    original_digest = hashlib.sha256(original).hexdigest()

    await _put_json_retry_safe(store, key=key, payload=original, digest=original_digest)
    await _put_json_retry_safe(store, key=key, payload=original, digest=original_digest)

    changed = b'{"records":[{"id":"changed"}]}'
    with pytest.raises(ValueError, match="differs from the original write"):
        await _put_json_retry_safe(
            store,
            key=key,
            payload=changed,
            digest=hashlib.sha256(changed).hexdigest(),
        )
    assert await store.get(key) == original


async def test_export_cleanup_refuses_another_tenants_object_key() -> None:
    store = MemoryObjectStore()
    other = uuid.uuid4()
    foreign_key = f"data-exports/{other}/{uuid.uuid4()}/manifest.json"
    await store.put(foreign_key, b"foreign")

    with pytest.raises(ValueError, match="does not belong"):
        await delete_export_objects(store, [foreign_key], organization_id=ORG)
    assert await store.get(foreign_key) == b"foreign"
