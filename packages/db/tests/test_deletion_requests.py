"""Durable deletion approval and legal-hold lifecycle tests."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.deletion_requests import (
    DOCUMENT_DELETION_JOB_TYPE,
    DeletionLifecycleError,
    DeletionRequestRepository,
    DeletionRequestState,
    approve_document_deletion,
    cancel_document_deletion,
    place_legal_hold,
    release_legal_hold,
    request_document_deletion,
)
from soa_db.documents import DocumentState, SourceChannel, create_document, transition_document
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/deletion-request.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def _settled_document(db: DatabaseSessions) -> uuid.UUID:
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="order.pdf",
            content_sha256="a" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:requester",
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
            to_state=DocumentState.QUARANTINED,
            actor_id="worker:test",
        )
        return document.id


async def test_request_needs_settled_document(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="active.pdf",
            content_sha256="b" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:requester",
        )
        with pytest.raises(DeletionLifecycleError, match="settled terminal state"):
            await request_document_deletion(
                session,
                CONTEXT,
                document_id=document.id,
                reason="verified erasure request",
                actor_id="user:requester",
            )


async def test_two_person_approval_enqueues_one_durable_job(db: DatabaseSessions) -> None:
    document_id = await _settled_document(db)
    async with db.session_scope() as session:
        first = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document_id,
            reason="verified erasure request",
            actor_id="user:requester",
        )
        repeated = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document_id,
            reason="duplicate delivery",
            actor_id="user:requester",
        )
        assert repeated.id == first.id
        with pytest.raises(DeletionLifecycleError, match="cannot approve"):
            await approve_document_deletion(
                session,
                CONTEXT,
                request=first,
                approval_reason="authority verified",
                actor_id="user:requester",
            )
        approved = await approve_document_deletion(
            session,
            CONTEXT,
            request=first,
            approval_reason="authority verified",
            actor_id="user:approver",
        )
        assert approved.state == DeletionRequestState.APPROVED.value

    async with db.session_scope() as session:
        jobs = (
            (await session.execute(select(Job).where(Job.job_type == DOCUMENT_DELETION_JOB_TYPE)))
            .scalars()
            .all()
        )
        assert len(jobs) == 1
        assert jobs[0].organization_id == ORG
        assert jobs[0].payload["deletion_request_id"] == str(first.id)


async def test_legal_hold_is_absolute_and_requires_fresh_approval_after_release(
    db: DatabaseSessions,
) -> None:
    document_id = await _settled_document(db)
    async with db.session_scope() as session:
        request = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document_id,
            reason="verified erasure request",
            actor_id="user:requester",
        )
        hold = await place_legal_hold(
            session,
            CONTEXT,
            document_id=document_id,
            reason="active litigation matter",
            actor_id="user:counsel",
        )
        with pytest.raises(DeletionLifecycleError, match="legal hold"):
            await approve_document_deletion(
                session,
                CONTEXT,
                request=request,
                approval_reason="authority verified",
                actor_id="user:approver",
            )
        await release_legal_hold(
            session,
            CONTEXT,
            hold=hold,
            reason="matter closed",
            actor_id="user:counsel",
        )
        await approve_document_deletion(
            session,
            CONTEXT,
            request=request,
            approval_reason="hold release independently verified",
            actor_id="user:approver",
        )
        await place_legal_hold(
            session,
            CONTEXT,
            document_id=document_id,
            reason="new preservation notice",
            actor_id="user:counsel",
        )
        assert request.state == DeletionRequestState.PENDING_APPROVAL.value
        assert request.approved_by is None

    async with db.session_scope() as session:
        stored = await DeletionRequestRepository(session, CONTEXT).get_for_document(document_id)
        assert stored is not None
        assert stored.state == DeletionRequestState.PENDING_APPROVAL.value


async def test_cancelled_request_can_be_reopened_without_competing_rows(
    db: DatabaseSessions,
) -> None:
    document_id = await _settled_document(db)
    async with db.session_scope() as session:
        request = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document_id,
            reason="first verified request",
            actor_id="user:first",
        )
        request_id = request.id
        await cancel_document_deletion(
            session,
            CONTEXT,
            request=request,
            reason="request scope was incorrect",
            actor_id="user:first",
        )
        assert request.state == DeletionRequestState.CANCELLED.value
        reopened = await request_document_deletion(
            session,
            CONTEXT,
            document_id=document_id,
            reason="corrected verified request",
            actor_id="user:second",
        )
        assert reopened.id == request_id
        assert reopened.state == DeletionRequestState.PENDING_APPROVAL.value
        assert reopened.reason == "corrected verified request"
        assert reopened.cancelled_at is None
