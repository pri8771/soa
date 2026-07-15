import uuid
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.data_export_jobs import DataExportJobRepository, new_data_export_job
from soa_db.documents import SourceChannel, create_document
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore
from soa_worker.data_export_orchestrator import execute_data_export_batch

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/large-export.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_organization_export_builds_bounded_parts_and_manifest(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
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

    store = MemoryObjectStore()
    async with db.session_scope() as session:
        result = await execute_data_export_batch(session, store, CONTEXT, export_id=export_id)
        assert result.complete is True and result.processed == 2

    async with db.session_scope() as session:
        stored = await DataExportJobRepository(session, CONTEXT).get(export_id)
        assert stored is not None and stored.state == "succeeded"
        assert stored.processed_documents == stored.total_documents == 2
        assert len(stored.parts) == 2
        assert stored.manifest_object_key is not None
        manifest = await store.get(stored.manifest_object_key)
        assert b'"total_documents": 2' in manifest
