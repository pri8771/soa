"""State projection tests (PRC-002): the COMPLETE transition matrix,
event replay with continuity checks, drift detection, correlation."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import update

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import (
    ALLOWED_TRANSITIONS,
    Document,
    DocumentRepository,
    DocumentState,
    InvalidDocumentTransitionError,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.repository import OrganizationContext
from soa_db.state_projection import (
    ProjectionError,
    project_document_state,
    verify_state_projection,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
STREAM = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONTEXT = OrganizationContext(organization_id=ORG)
SHA = "a" * 64


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/projection.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_document(db: DatabaseSessions) -> uuid.UUID:
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=SHA,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        return document.id


async def test_complete_transition_matrix(db: DatabaseSessions) -> None:
    """Every (from, to) pair behaves exactly as the map says — allowed
    pairs flush, everything else is refused by the guard."""
    document_id = await make_document(db)
    states = list(DocumentState)
    checked = 0
    for source in states:
        for target in states:
            if source == target:
                continue
            expected_allowed = target in ALLOWED_TRANSITIONS.get(source, frozenset())
            async with db.session_scope() as session:
                # Plant the source state via Core (bypasses ORM guards, as
                # a matrix harness must).
                await session.execute(
                    update(Document).where(Document.id == document_id).values(state=source.value)
                )
            if expected_allowed:
                async with db.session_scope() as session:
                    document = await DocumentRepository(session, CONTEXT).get(document_id)
                    assert document is not None
                    document.state = target.value
                    await session.flush()
            else:
                with pytest.raises(InvalidDocumentTransitionError):
                    async with db.session_scope() as session:
                        document = await DocumentRepository(session, CONTEXT).get(document_id)
                        assert document is not None
                        document.state = target.value
                        await session.flush()
            checked += 1
    assert checked == len(states) * (len(states) - 1)


async def test_projection_replays_the_trail_with_actor_reason_correlation(
    db: DatabaseSessions,
) -> None:
    document_id = await make_document(db)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="system:file-inspection",
            correlation_id="corr-abc",
        )
        await transition_document(
            session,
            CONTEXT,
            document=document,
            to_state=DocumentState.QUARANTINED,
            reason="malware detected: Win.Test",
            actor_id="system:malware-scan",
            correlation_id="corr-abc",
        )

    async with db.session_scope() as session:
        projected = await project_document_state(session, CONTEXT, document_id)
        assert projected.state == "quarantined"
        assert [(t.from_state, t.to_state) for t in projected.history] == [
            ("received", "validating_file"),
            ("validating_file", "quarantined"),
        ]
        last = projected.history[-1]
        assert last.actor_id == "system:malware-scan"
        assert last.reason == "malware detected: Win.Test"
        assert last.correlation_id == "corr-abc"

        # Projection agrees with the stored column.
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        verified = await verify_state_projection(session, CONTEXT, document)
        assert verified.state == document.state


async def test_projection_detects_drift_and_gaps(db: DatabaseSessions) -> None:
    document_id = await make_document(db)
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

    # Drift: a write path that skipped the transition service.
    async with db.session_scope() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(state=DocumentState.COMPLETED.value)
        )
    async with db.session_scope() as session:
        document = await DocumentRepository(session, CONTEXT).get(document_id)
        assert document is not None
        with pytest.raises(ProjectionError, match="disagrees"):
            await verify_state_projection(session, CONTEXT, document)

    # A document with no received event has no coherent history.
    async with db.session_scope() as session:
        with pytest.raises(ProjectionError, match="document.received"):
            await project_document_state(session, CONTEXT, uuid.uuid4())
