"""Worker execution for durable approved document-deletion requests."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.data_deletion import DeletionTombstone
from soa_db.deletion_requests import (
    DeletionLifecycleError,
    DeletionRequestRepository,
    DeletionRequestState,
    approve_document_deletion,
    place_legal_hold,
    request_document_deletion,
)
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.repository import OrganizationContext
from soa_db.state_projection import verify_state_projection
from soa_storage import MemoryObjectStore, sha256_hex
from soa_worker.document_deletion import execute_document_deletion

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/worker-deletion.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _approved_request(
    db: DatabaseSessions,
    store: MemoryObjectStore,
    *,
    settled_state: DocumentState = DocumentState.QUARANTINED,
) -> tuple[uuid.UUID, uuid.UUID]:
    content = b"deletable sales order"
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="order.pdf",
            content_sha256=sha256_hex(content),
            size_bytes=len(content),
            content_type="application/pdf",
            actor_id="user:requester",
        )
        key = f"orgs/{ORG}/documents/{document.id}/original/order.pdf"
        await store.put(key, content, content_type="application/pdf")
        await create_artifact(
            session,
            CONTEXT,
            document_id=document.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=sha256_hex(content),
            size_bytes=len(content),
            content_type="application/pdf",
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker:test",
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=settled_state,
            actor_id="worker:test",
        )
        request = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document.id,
            reason="verified customer request",
            actor_id="user:requester",
        )
        await approve_document_deletion(
            session,
            CONTEXT,
            request=request,
            approval_reason="authority and scope verified",
            actor_id="user:approver",
        )
        return document.id, request.id


async def test_worker_completes_request_and_redelivery_is_a_no_op(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    document_id, request_id = await _approved_request(db, store)

    assert (
        await execute_document_deletion(
            db,
            store,
            organization_id=ORG,
            deletion_request_id=request_id,
        )
        == DeletionRequestState.COMPLETED.value
    )
    assert (
        await execute_document_deletion(
            db,
            store,
            organization_id=ORG,
            deletion_request_id=request_id,
        )
        == DeletionRequestState.COMPLETED.value
    )
    assert not store._objects  # type: ignore[attr-defined]

    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get(request_id)
        assert request is not None
        assert request.state == DeletionRequestState.COMPLETED.value
        assert request.completed_at is not None
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == DocumentState.DELETED.value
        projection = await verify_state_projection(session, CONTEXT, document)
        assert projection.state == DocumentState.DELETED.value
        repeated = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document_id,
            reason="redelivered verified request",
            actor_id="user:requester",
        )
        assert repeated.id == request_id
        assert repeated.state == DeletionRequestState.COMPLETED.value
        with pytest.raises(DeletionLifecycleError, match="deleted"):
            await place_legal_hold(
                session,
                CONTEXT,
                document_id=document_id,
                reason="late preservation notice",
                actor_id="user:counsel",
            )
        tombstone = (await session.execute(select(DeletionTombstone))).scalar_one()
        assert tombstone is not None and tombstone.document_id == document_id


async def test_worker_clears_approval_if_document_resumes_before_execution(
    db: DatabaseSessions,
) -> None:
    store = MemoryObjectStore()
    document_id, request_id = await _approved_request(
        db, store, settled_state=DocumentState.FAILED_TERMINAL
    )
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.QUEUED,
            reason="configuration repaired before queued deletion ran",
            actor_id="user:operator",
        )

    assert (
        await execute_document_deletion(
            db,
            store,
            organization_id=ORG,
            deletion_request_id=request_id,
        )
        == DeletionRequestState.PENDING_APPROVAL.value
    )
    assert store._objects  # type: ignore[attr-defined]

    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get(request_id)
        assert request is not None
        assert request.state == DeletionRequestState.PENDING_APPROVAL.value
        assert request.approved_by is None
        assert request.approval_reason is None
        assert request.approved_at is None
        assert request.safe_error is not None
        assert "no longer" in request.safe_error
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        assert document.state == DocumentState.QUEUED.value


async def test_failed_external_delete_is_persisted_and_retry_resumes(
    db: DatabaseSessions,
) -> None:
    class FailingOnceStore(MemoryObjectStore):
        fail = True

        async def delete(self, key: str) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("object store unavailable")
            await super().delete(key)

    store = FailingOnceStore()
    _, request_id = await _approved_request(db, store)
    with pytest.raises(RuntimeError, match="unavailable"):
        await execute_document_deletion(
            db,
            store,
            organization_id=ORG,
            deletion_request_id=request_id,
        )
    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get(request_id)
        assert request is not None
        assert request.state == DeletionRequestState.FAILED.value
        assert request.safe_error == "document deletion failed (RuntimeError)"

    assert (
        await execute_document_deletion(
            db,
            store,
            organization_id=ORG,
            deletion_request_id=request_id,
        )
        == DeletionRequestState.COMPLETED.value
    )
