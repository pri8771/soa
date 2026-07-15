"""Abandoned direct-upload cleanup is durable, tenant-scoped, and idempotent."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow
from soa_db.uploads import (
    UploadSessionRepository,
    UploadSessionState,
    create_upload_session,
)
from soa_storage import MemoryObjectStore
from soa_worker.upload_cleanup import UploadCleanupNotDueError, cleanup_expired_upload

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/upload-cleanup.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _session(db: DatabaseSessions, *, ttl_seconds: int = 60) -> tuple[uuid.UUID, str]:
    async with db.session_scope() as session:
        record = await create_upload_session(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            object_key=f"orgs/{ORG}/documents/{uuid.uuid4()}/original/upload.pdf",
            filename="upload.pdf",
            content_type="application/pdf",
            size_bytes=7,
            sha256="a" * 64,
            client_reference=None,
            ttl_seconds=ttl_seconds,
            actor_id="user:test",
        )
        return record.id, record.object_key


async def test_cleanup_deletes_bytes_expires_row_and_is_idempotent(
    db: DatabaseSessions,
) -> None:
    session_id, key = await _session(db)
    store = MemoryObjectStore()
    await store.put(key, b"payload", content_type="application/pdf")
    async with db.session_scope() as session:
        record = await UploadSessionRepository(session, CONTEXT).get(session_id)
        assert record is not None
        record.expires_at = utcnow() - timedelta(seconds=1)

    assert (
        await cleanup_expired_upload(
            db,
            store,
            organization_id=ORG,
            upload_session_id=session_id,
        )
        == UploadSessionState.EXPIRED.value
    )
    assert await store.list_keys() == []

    # A request admitted just before URL expiry may finalize after the first
    # pass. The scheduled sweep revisits terminal expired sessions and removes
    # those late bytes without changing their state.
    await store.put(key, b"late payload", content_type="application/pdf")
    assert (
        await cleanup_expired_upload(
            db,
            store,
            organization_id=ORG,
            upload_session_id=session_id,
        )
        == UploadSessionState.EXPIRED.value
    )
    assert await store.list_keys() == []
    assert (
        await cleanup_expired_upload(
            db,
            store,
            organization_id=OTHER_ORG,
            upload_session_id=session_id,
        )
        == "missing"
    )


async def test_cleanup_never_acknowledges_a_session_before_expiry(
    db: DatabaseSessions,
) -> None:
    session_id, key = await _session(db, ttl_seconds=3_600)
    store = MemoryObjectStore()
    await store.put(key, b"payload", content_type="application/pdf")

    with pytest.raises(UploadCleanupNotDueError):
        await cleanup_expired_upload(
            db,
            store,
            organization_id=ORG,
            upload_session_id=session_id,
        )
    assert await store.list_keys() == [key]
