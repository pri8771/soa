"""Retention sweep: a settled document past its org's published retention
window gets an automatic, pending-approval deletion request — never an
automatic approval or deletion; legal holds and non-standard artifact
classes are left for a human."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from soa_api.domain.policies import PolicyType, PolicyVersion
from soa_api.domain.tenancy import Organization, OrganizationStatus
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, RetentionClass, create_artifact
from soa_db.deletion_requests import DeletionRequestRepository, place_legal_hold
from soa_db.documents import (
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow
from soa_storage.keys import artifact_key
from soa_storage.memory import MemoryObjectStore
from soa_storage.store import sha256_hex
from soa_worker.retention_scheduler import RETENTION_SCHEDULER_ACTOR, RetentionCoordinator

ORG = uuid.UUID("21111111-1111-4211-8211-111111111111")
STREAM = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/retention.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_org_with_policy(db: DatabaseSessions, *, document_days: int) -> None:
    async with db.session_scope() as session:
        session.add(
            Organization(
                id=ORG, name="Northstar", slug="northstar", status=OrganizationStatus.ACTIVE
            )
        )
        session.add(
            PolicyVersion(
                organization_id=ORG,
                policy_type=PolicyType.RETENTION.value,
                version_number=1,
                definition={"document_days": document_days},
                state="published",
            )
        )


async def seed_document(
    db: DatabaseSessions,
    *,
    state: DocumentState,
    settled_days_ago: int,
    retention_class: RetentionClass = RetentionClass.STANDARD,
) -> uuid.UUID:
    store = MemoryObjectStore()
    data = b"%PDF-1.4 test"
    sha = sha256_hex(data)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=STREAM,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256=sha,
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        key = artifact_key(ORG, document.id, kind="original", filename="po.pdf")
        await store.put(key, data, content_type="application/pdf")
        await create_artifact(
            session,
            CONTEXT,
            document_id=document.id,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=sha,
            size_bytes=len(data),
            content_type="application/pdf",
            retention_class=retention_class,
        )
        for intermediate in (
            DocumentState.VALIDATING_FILE,
            DocumentState.QUEUED,
            DocumentState.PREPROCESSING,
            DocumentState.CLASSIFYING,
            DocumentState.SPLITTING,
            DocumentState.EXTRACTING,
            DocumentState.NORMALIZING,
            DocumentState.VALIDATING_DATA,
            DocumentState.APPROVED,
            DocumentState.EXPORTING,
        ):
            await transition_document(
                session, CONTEXT, document=document, to_state=intermediate, actor_id="worker"
            )
        await transition_document(
            session, CONTEXT, document=document, to_state=state, actor_id="worker"
        )
        # Simulate settlement having happened `settled_days_ago` days ago —
        # the sweep approximates settled_at as `updated_at` (documented).
        document.updated_at = utcnow() - timedelta(days=settled_days_ago)
        await session.flush()
        return document.id


async def test_eligible_settled_document_gets_an_automatic_pending_request(
    db: DatabaseSessions,
) -> None:
    await seed_org_with_policy(db, document_days=30)
    document_id = await seed_document(db, state=DocumentState.COMPLETED, settled_days_ago=31)

    created = await RetentionCoordinator(db).reconcile()
    assert created == 1

    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get_for_document(document_id)
        assert request is not None
        assert request.state == "pending_approval"
        assert request.requested_by == RETENTION_SCHEDULER_ACTOR
        assert "30-day retention window elapsed" in request.reason


async def test_document_still_within_the_window_is_left_alone(db: DatabaseSessions) -> None:
    await seed_org_with_policy(db, document_days=30)
    document_id = await seed_document(db, state=DocumentState.COMPLETED, settled_days_ago=5)

    created = await RetentionCoordinator(db).reconcile()
    assert created == 0

    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get_for_document(document_id)
        assert request is None


async def test_organization_without_a_published_policy_is_skipped(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        session.add(
            Organization(
                id=ORG, name="Northstar", slug="northstar", status=OrganizationStatus.ACTIVE
            )
        )
    await seed_document(db, state=DocumentState.COMPLETED, settled_days_ago=9999)

    created = await RetentionCoordinator(db).reconcile()
    assert created == 0


async def test_legal_hold_blocks_the_automatic_request(db: DatabaseSessions) -> None:
    await seed_org_with_policy(db, document_days=30)
    document_id = await seed_document(db, state=DocumentState.COMPLETED, settled_days_ago=90)
    async with db.session_scope() as session:
        await place_legal_hold(
            session,
            CONTEXT,
            document_id=document_id,
            reason="Ongoing dispute",
            actor_id="user:legal",
        )

    created = await RetentionCoordinator(db).reconcile()
    assert created == 0


async def test_non_standard_artifact_class_is_left_for_a_human(db: DatabaseSessions) -> None:
    await seed_org_with_policy(db, document_days=30)
    document_id = await seed_document(
        db,
        state=DocumentState.COMPLETED,
        settled_days_ago=90,
        retention_class=RetentionClass.EXTENDED,
    )

    created = await RetentionCoordinator(db).reconcile()
    assert created == 0

    async with db.session_scope() as session:
        request = await DeletionRequestRepository(session, CONTEXT).get_for_document(document_id)
        assert request is None


async def test_already_requested_documents_are_not_requested_again(db: DatabaseSessions) -> None:
    await seed_org_with_policy(db, document_days=30)
    await seed_document(db, state=DocumentState.COMPLETED, settled_days_ago=31)

    first = await RetentionCoordinator(db).reconcile()
    second = await RetentionCoordinator(db).reconcile()
    assert first == 1
    assert second == 0
