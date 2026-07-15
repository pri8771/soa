"""Cross-replica PostgreSQL proofs for transaction and lease fencing."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from soa_api.domain.policies import PolicyType, PolicyVersion, create_policy_draft
from soa_api.domain.uploads import (
    UploadPolicyError,
    UploadSession,
    UploadSessionRepository,
    create_upload_session,
    validate_upload_declaration,
)
from soa_api.services.duplicates import (
    DuplicatePolicy,
    find_exact_duplicate,
    lock_exact_duplicate_intake,
    mark_duplicate,
)
from soa_config import MemorySecretStore
from soa_db import DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.deletion_requests import (
    DeletionLifecycleError,
    DeletionRequest,
    DeletionRequestRepository,
    DeletionRequestState,
    LegalHold,
    LegalHoldRepository,
    approve_document_deletion,
    place_legal_hold,
    request_document_deletion,
)
from soa_db.documents import (
    Document,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import (
    Job,
    JobLockError,
    JobStatus,
    claim_next_jobs,
    enqueue_job,
    recover_expired_locks,
)
from soa_db.provider_credentials import ProviderCredential, store_provider_credential
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow
from soa_worker.database_queue import DatabaseJobQueue

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; concurrency fencing requires PostgreSQL",
    ),
]


@pytest.fixture
async def replicas() -> AsyncIterator[tuple[DatabaseSessions, DatabaseSessions]]:
    assert POSTGRES_URL is not None
    first = DatabaseSessions(create_database_engine(POSTGRES_URL))
    second = DatabaseSessions(create_database_engine(POSTGRES_URL))
    yield first, second
    await first.dispose()
    await second.dispose()


async def test_pending_upload_cap_is_atomic_across_replicas(
    replicas: tuple[DatabaseSessions, DatabaseSessions],
) -> None:
    first, second = replicas
    organization_id = uuid.uuid4()
    context = OrganizationContext(organization_id)

    async def reserve(db: DatabaseSessions, suffix: str) -> bool:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            repository = UploadSessionRepository(session, context)
            await repository.lock_pending_quota()
            pending = await repository.count_pending()
            try:
                validate_upload_declaration(
                    content_type="application/pdf",
                    size_bytes=10,
                    pending_sessions=pending,
                    max_size_bytes=100,
                    max_pending_sessions=1,
                )
            except UploadPolicyError:
                return False
            await create_upload_session(
                session,
                context,
                stream_id=uuid.uuid4(),
                object_key=f"orgs/{organization_id}/concurrency/{suffix}",
                filename=f"{suffix}.pdf",
                content_type="application/pdf",
                size_bytes=10,
                sha256=suffix.ljust(64, "a")[:64],
                client_reference=None,
                ttl_seconds=300,
                actor_id="user:concurrency-test",
            )
            return True

    try:
        decisions = await asyncio.gather(reserve(first, "first"), reserve(second, "second"))
        assert decisions.count(True) == 1
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            assert await UploadSessionRepository(session, context).count_pending() == 1
    finally:
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            await session.execute(
                delete(UploadSession).where(UploadSession.organization_id == organization_id)
            )


async def test_exact_duplicate_registration_is_serialized_across_replicas(
    replicas: tuple[DatabaseSessions, DatabaseSessions],
) -> None:
    """Both transactions start together; exactly one becomes the original."""

    first, second = replicas
    organization_id = uuid.uuid4()
    context = OrganizationContext(organization_id)
    stream_id = uuid.uuid4()
    content_sha256 = "d" * 64
    rendezvous = asyncio.Barrier(2)

    async def register(db: DatabaseSessions, suffix: str) -> uuid.UUID:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            await rendezvous.wait()
            await lock_exact_duplicate_intake(
                session,
                context,
                stream_id=stream_id,
                content_sha256=content_sha256,
            )
            # Keep the winner's transaction open briefly so its peer is
            # demonstrably waiting on the advisory lock, not merely scheduled later.
            await asyncio.sleep(0.05)
            document = await create_document(
                session,
                context,
                stream_id=stream_id,
                source_channel=SourceChannel.UPLOAD,
                original_filename=f"{suffix}.pdf",
                content_sha256=content_sha256,
                size_bytes=100,
                content_type="application/pdf",
                actor_id="user:concurrency-test",
            )
            original = await find_exact_duplicate(
                session,
                context,
                stream_id=stream_id,
                content_sha256=content_sha256,
                exclude_document_id=document.id,
            )
            if original is not None:
                await mark_duplicate(
                    session,
                    context,
                    document=document,
                    original=original,
                    policy=DuplicatePolicy.FLAG,
                    actor_id="system:duplicate-detection",
                )
            return document.id

    try:
        document_ids = await asyncio.gather(register(first, "first"), register(second, "second"))
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            documents = list(
                (
                    await session.execute(
                        select(Document).where(
                            Document.organization_id == organization_id,
                            Document.id.in_(document_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            originals = [document for document in documents if document.duplicate_of is None]
            duplicates = [document for document in documents if document.duplicate_of is not None]
            assert len(originals) == 1
            assert len(duplicates) == 1
            assert duplicates[0].duplicate_of == originals[0].id
            events = list(
                (
                    await session.execute(
                        select(AuditEvent).where(
                            AuditEvent.organization_id == organization_id,
                            AuditEvent.action == "document.duplicate_detected",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1
    finally:
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            await session.execute(
                delete(Document).where(Document.organization_id == organization_id)
            )
            await session.execute(
                delete(AuditEvent).where(AuditEvent.organization_id == organization_id)
            )


async def test_provider_rotation_and_policy_version_allocation_are_serialized(
    replicas: tuple[DatabaseSessions, DatabaseSessions],
) -> None:
    first, second = replicas
    organization_id = uuid.uuid4()
    context = OrganizationContext(organization_id)
    secrets = MemorySecretStore()

    async def rotate(db: DatabaseSessions, suffix: str) -> None:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            await store_provider_credential(
                session,
                context,
                provider_name="anthropic-claude",
                label=f"Claude {suffix}",
                kind="api_key",
                secret=f"provider-secret-{suffix}",
                actor_id="user:concurrency-test",
                secret_store=secrets,
            )

    async def draft(db: DatabaseSessions, days: int) -> None:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            await create_policy_draft(
                session,
                context,
                policy_type=PolicyType.RETENTION,
                definition={"document_days": days},
                actor_id="user:concurrency-test",
            )

    try:
        await asyncio.gather(rotate(first, "one"), rotate(second, "two"))
        await asyncio.gather(draft(first, 30), draft(second, 60))
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            credentials = list(
                (
                    await session.execute(
                        select(ProviderCredential).where(
                            ProviderCredential.organization_id == organization_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(credentials) == 2
            assert sum(row.superseded_at is None for row in credentials) == 1
            versions = list(
                (
                    await session.execute(
                        select(PolicyVersion).where(
                            PolicyVersion.organization_id == organization_id,
                            PolicyVersion.policy_type == PolicyType.RETENTION,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert sorted(row.version_number for row in versions) == [1, 2]
    finally:
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            await session.execute(
                delete(ProviderCredential).where(
                    ProviderCredential.organization_id == organization_id
                )
            )
            await session.execute(
                delete(PolicyVersion).where(PolicyVersion.organization_id == organization_id)
            )


async def test_legal_hold_and_approval_race_always_ends_blocked(
    replicas: tuple[DatabaseSessions, DatabaseSessions],
) -> None:
    first, second = replicas
    organization_id = uuid.uuid4()
    context = OrganizationContext(organization_id)
    async with first.session_scope() as session:
        await bind_tenant(session, organization_id)
        document = await create_document(
            session,
            context,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="concurrency.pdf",
            content_sha256="c" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:requester",
        )
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.VALIDATING_FILE,
            actor_id="worker:test",
        )
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.QUARANTINED,
            actor_id="worker:test",
        )
        request = await request_document_deletion(
            session,
            context,
            document_id=document.id,
            reason="verified concurrency request",
            actor_id="user:requester",
        )
        document_id, request_id = document.id, request.id

    async def approve() -> str:
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            request = await DeletionRequestRepository(session, context).get(request_id)
            assert request is not None
            try:
                approved = await approve_document_deletion(
                    session,
                    context,
                    request=request,
                    approval_reason="independently verified",
                    actor_id="user:approver",
                )
            except DeletionLifecycleError:
                return "blocked"
            return approved.state

    async def hold() -> str:
        async with second.session_scope() as session:
            await bind_tenant(session, organization_id)
            held = await place_legal_hold(
                session,
                context,
                document_id=document_id,
                reason="litigation preservation",
                actor_id="user:counsel",
            )
            return held.state

    try:
        await asyncio.gather(approve(), hold())
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            request = await DeletionRequestRepository(session, context).get(request_id)
            active_hold = await LegalHoldRepository(session, context).active_for_document(
                document_id
            )
            assert active_hold is not None
            assert request is not None
            assert request.state == DeletionRequestState.PENDING_APPROVAL.value
            assert request.approved_by is None
    finally:
        async with first.session_scope() as session:
            await bind_tenant(session, organization_id)
            await session.execute(
                delete(LegalHold).where(LegalHold.organization_id == organization_id)
            )
            await session.execute(
                delete(DeletionRequest).where(DeletionRequest.organization_id == organization_id)
            )
            await session.execute(
                delete(Document).where(Document.organization_id == organization_id)
            )
            await session.execute(
                delete(AuditEvent).where(AuditEvent.organization_id == organization_id)
            )
        async with first.session_scope() as session:
            await session.execute(delete(Job).where(Job.organization_id == organization_id))


async def test_stale_ack_blocks_behind_reclaim_then_rejects_same_worker_attempt(
    replicas: tuple[DatabaseSessions, DatabaseSessions],
) -> None:
    first, second = replicas
    worker_id = f"worker:shared:{uuid.uuid4()}"
    queue = DatabaseJobQueue(first, worker_id=worker_id)
    async with first.session_scope() as session:
        job = await enqueue_job(session, job_type="postgres.lease-fence", payload={})
        await session.flush()
        job_id = job.id

    stale = await queue.claim()
    assert stale is not None and stale.lease_attempt == 1
    acknowledgement: asyncio.Task[None] | None = None
    try:
        async with second.session_scope() as session:
            reclaimed_at = utcnow() + timedelta(minutes=6)
            assert len(await recover_expired_locks(session, now=reclaimed_at)) == 1
            reclaimed = await claim_next_jobs(
                session,
                worker_id=worker_id,
                now=reclaimed_at,
            )
            assert len(reclaimed) == 1 and reclaimed[0].attempts == 2
            acknowledgement = asyncio.create_task(queue.succeeded(stale))
            await asyncio.sleep(0.05)
            assert not acknowledgement.done(), "stale acknowledgement must wait for row owner"

        assert acknowledgement is not None
        with pytest.raises(JobLockError, match="stale lease"):
            await acknowledgement
        async with first.session_scope() as session:
            record = await session.get(Job, job_id)
            assert record is not None
            assert record.status == JobStatus.RUNNING
            assert record.attempts == 2
            assert record.lock_owner == worker_id
    finally:
        if acknowledgement is not None and not acknowledgement.done():
            acknowledgement.cancel()
        async with first.session_scope() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
