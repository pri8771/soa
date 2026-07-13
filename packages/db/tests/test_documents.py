"""Document model tests (ING-001): constrained state machine, client
idempotency, source-metadata sanitization, tenant scope."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.documents import (
    ALLOWED_TRANSITIONS,
    Document,
    DocumentRepository,
    DocumentState,
    InvalidDocumentTransitionError,
    SourceChannel,
    create_document,
    sanitize_source_metadata,
    transition_document,
)
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
SHA = "d" * 64
CONTEXT = OrganizationContext(organization_id=ORG_A)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/documents.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_document(
    db: DatabaseSessions,
    *,
    client_reference: str | None = None,
    stream_id: uuid.UUID = STREAM,
    org: uuid.UUID = ORG_A,
) -> uuid.UUID:
    async with db.session_scope() as session:
        document = await create_document(
            session,
            OrganizationContext(organization_id=org),
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po-4711.pdf",
            content_sha256=SHA,
            size_bytes=1024,
            content_type="application/pdf",
            client_reference=client_reference,
            source_metadata={"uploader": "user:reviewer"},
            actor_id="user:reviewer",
        )
        return document.id


async def test_create_records_stream_scope_and_audit(db: DatabaseSessions) -> None:
    document_id = await make_document(db)
    async with db.session_scope() as session:
        stored = await DocumentRepository(session, CONTEXT).get(document_id)
        assert stored is not None
        assert stored.state == DocumentState.RECEIVED.value
        assert stored.stream_id == STREAM
        assert stored.organization_id == ORG_A
        assert stored.source_metadata == {"uploader": "user:reviewer"}
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.received")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


async def test_happy_path_transitions_are_allowed_and_audited(db: DatabaseSessions) -> None:
    document_id = await make_document(db)
    chain = [
        DocumentState.VALIDATING_FILE,
        DocumentState.QUEUED,
        DocumentState.PREPROCESSING,
        DocumentState.CLASSIFYING,
        DocumentState.SPLITTING,
        DocumentState.EXTRACTING,
        DocumentState.NORMALIZING,
        DocumentState.VALIDATING_DATA,
        DocumentState.REVIEW_REQUIRED,
        DocumentState.APPROVED,
        DocumentState.EXPORTING,
        DocumentState.COMPLETED,
        DocumentState.ARCHIVED,
    ]
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        for state in chain:
            await transition_document(
                session, CONTEXT, document=document, to_state=state, actor_id="worker"
            )
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "document.state_changed")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == len(chain)


async def test_illegal_jumps_are_refused_even_via_raw_assignment(db: DatabaseSessions) -> None:
    document_id = await make_document(db)
    # received -> extracting skips validation and queueing: refused.
    with pytest.raises(InvalidDocumentTransitionError):
        async with db.session_scope() as session:
            document = await DocumentRepository(session, CONTEXT).get(document_id)
            assert document is not None
            document.state = DocumentState.EXTRACTING.value  # raw write, no helper
            await session.flush()
    # Archived is terminal.
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker",
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.QUARANTINED,
            reason="EICAR signature",
            actor_id="worker",
        )
        await transition_document(
            session, CONTEXT, document=document, to_state=DocumentState.ARCHIVED, actor_id="worker"
        )
    with pytest.raises(InvalidDocumentTransitionError):
        async with db.session_scope() as session:
            document = await DocumentRepository(session, CONTEXT).get(document_id)
            assert document is not None
            document.state = DocumentState.QUEUED.value
            await session.flush()


def test_retryable_failure_reenters_the_queue() -> None:
    assert DocumentState.QUEUED in ALLOWED_TRANSITIONS[DocumentState.FAILED_RETRYABLE]
    assert DocumentState.FAILED_TERMINAL in ALLOWED_TRANSITIONS[DocumentState.FAILED_RETRYABLE]
    # Terminal states never resume processing.
    assert DocumentState.QUEUED not in ALLOWED_TRANSITIONS.get(
        DocumentState.FAILED_TERMINAL, frozenset()
    )
    assert ALLOWED_TRANSITIONS.get(DocumentState.ARCHIVED, frozenset()) == frozenset()


async def test_client_reference_idempotency_is_scoped(db: DatabaseSessions) -> None:
    await make_document(db, client_reference="po-batch-1")
    # Same org+stream+reference: refused by the partial unique index.
    with pytest.raises(IntegrityError):
        await make_document(db, client_reference="po-batch-1")
    # Different stream: fine.
    await make_document(db, client_reference="po-batch-1", stream_id=uuid.uuid4())
    # Missing references never collide.
    await make_document(db)
    await make_document(db)

    async with db.session_scope() as session:
        found = await DocumentRepository(session, CONTEXT).get_by_client_reference(
            STREAM, "po-batch-1"
        )
        assert found is not None


async def test_source_metadata_is_allowlisted_and_bounded() -> None:
    sanitized = sanitize_source_metadata(
        {
            "sender": "ap@customer.example",
            "subject": "s" * 1000,
            "raw_headers": "Received: from evil...",
            "body": "full message body",
            "nested": {"anything": "here"},
            "message_id": 42,
        }
    )
    assert set(sanitized) == {"sender", "subject", "message_id"}
    assert len(sanitized["subject"]) == 300
    assert sanitized["message_id"] == "42"
    assert sanitize_source_metadata(None) == {}


async def test_tenant_scope_hides_other_organizations(db: DatabaseSessions) -> None:
    document_id = await make_document(db)
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await DocumentRepository(session, other).get(document_id) is None
        assert await DocumentRepository(session, other).list_by_content_hash(SHA) == []
        mine = await DocumentRepository(session, CONTEXT).list_by_content_hash(SHA)
        assert [d.id for d in mine] == [document_id]


async def test_create_rejects_bad_inputs(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="content_sha256"):
            await create_document(
                session,
                CONTEXT,
                stream_id=STREAM,
                source_channel=SourceChannel.API,
                original_filename="x.pdf",
                content_sha256="nope",
                size_bytes=1,
                content_type="application/pdf",
                actor_id="svc:key",
            )
        with pytest.raises(ValueError, match="size_bytes"):
            await create_document(
                session,
                CONTEXT,
                stream_id=STREAM,
                source_channel=SourceChannel.API,
                original_filename="x.pdf",
                content_sha256=SHA,
                size_bytes=0,
                content_type="application/pdf",
                actor_id="svc:key",
            )


def test_documents_participate_in_rls() -> None:
    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert Document.__tablename__ in RLS_PROTECTED_TABLES
