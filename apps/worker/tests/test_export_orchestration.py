"""Export orchestration tests (EXP-008): happy path, receiver timeout,
retry with the fixed payload, terminal rejection, replay, and the
exactly-once no-op on settled jobs."""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from soa_config import MemorySecretStore
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactRepository
from soa_db.canonical_payloads import record_canonical_payload
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.exports import (
    DeliveryAttemptRepository,
    ExportJobRepository,
    create_export_job,
    replay_export_job,
)
from soa_db.integrations import (
    IntegrationStatus,
    create_integration,
    create_mapping_draft,
    publish_mapping_draft,
    store_integration_credential,
)
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore
from soa_worker.export_orchestrator import EXPORT_JOB_TYPE, execute_export
from soa_worker.export_runner import ExportDeliveryHandler
from soa_worker.webhook import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, verify_webhook_signature

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)
NOW = 1_800_000_000
ALLOWLIST = ["erp.northstar.example"]
SECRET = "whsec_orchestration_secret"

CANONICAL_PAYLOAD: dict[str, Any] = {
    "schema_version": "1.0.0",
    "identifiers": {"po_number": "PO-100042"},
    "dates": {"order_date": "2026-03-14"},
    "terms": {"currency": "USD"},
    "totals": {"grand_total": {"amount": "450.00", "currency": "USD"}},
    "line_items": [
        {
            "line_number": 1,
            "quantity": "10",
            "line_total": {"amount": "450.00", "currency": "USD"},
        }
    ],
    "source": {"document_id": "x", "run_id": "y"},
}
MAPPING = {
    "fields": [
        {"target": "PoNumber", "source": "identifiers.po_number", "required": True},
        {"target": "Total", "source": "totals.grand_total.amount"},
    ]
}


def public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/export-orch.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


#: Shared across seed() and run_export(): the worker resolves the
#: reference the seeding wrote (names are unique per credential).
SECRETS = MemorySecretStore()


async def seed(
    db: DatabaseSessions,
    *,
    with_credential: bool = True,
    integration_status: IntegrationStatus = IntegrationStatus.ACTIVE,
) -> uuid.UUID:
    """An APPROVED document with a canonical payload, a deliverable
    integration with a published mapping, and the export job."""
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="d" * 64,
            size_bytes=10,
            content_type="application/pdf",
            actor_id="user:test",
        )
        for state in (
            DocumentState.VALIDATING_FILE,
            DocumentState.QUEUED,
            DocumentState.PREPROCESSING,
            DocumentState.CLASSIFYING,
            DocumentState.SPLITTING,
            DocumentState.EXTRACTING,
            DocumentState.NORMALIZING,
            DocumentState.VALIDATING_DATA,
            DocumentState.APPROVED,
        ):
            await transition_document(
                session, CONTEXT, document=document, to_state=state, actor_id="system:test"
            )
        run_id = uuid.uuid4()
        payload_row = await record_canonical_payload(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run_id,
            task_id=None,
            schema_version="1.0.0",
            payload=dict(CANONICAL_PAYLOAD),
            actor_id="user:test",
        )
        integration = await create_integration(
            session,
            CONTEXT,
            name="ERP",
            slug="erp",
            integration_type="webhook",
            endpoint_url="https://erp.northstar.example/orders",
            actor_id="user:test",
        )
        integration.status = integration_status
        if with_credential:
            await store_integration_credential(
                session,
                CONTEXT,
                integration=integration,
                kind="webhook_hmac_secret",
                secret=SECRET,
                actor_id="user:test",
                secret_store=SECRETS,
            )
        draft = await create_mapping_draft(
            session,
            CONTEXT,
            integration=integration,
            definition=MAPPING,
            target_schema={"type": "object", "required": ["PoNumber"]},
            actor_id="user:test",
        )
        mapping = await publish_mapping_draft(
            session, CONTEXT, integration=integration, draft=draft, actor_id="user:test"
        )
        job = await create_export_job(
            session,
            CONTEXT,
            document_id=document.id,
            run_id=run_id,
            canonical_payload_id=payload_row.id,
            integration_id=integration.id,
            mapping_version_id=mapping.id,
            actor_id="system:test",
        )
        return job.id


def make_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run_export(
    db: DatabaseSessions,
    store: MemoryObjectStore,
    job_id: uuid.UUID,
    handler: Any,
    *,
    timestamp: int = NOW,
) -> Any:
    async with make_client(handler) as client:
        return await execute_export(
            db,
            store,
            CONTEXT,
            export_job_id=job_id,
            client=client,
            allowlist=ALLOWLIST,
            timestamp=timestamp,
            secret_store=SECRETS,
            resolve=public_resolver,
        )


async def job_and_document_state(db: DatabaseSessions, job_id: uuid.UUID) -> tuple[str, str, int]:
    async with db.session_scope() as session:
        job = await ExportJobRepository(session, CONTEXT).get(job_id)
        assert job is not None
        document = await DocumentRepository(session, CONTEXT).get(job.document_id)
        assert document is not None
        return job.state, document.state, job.attempt_count


async def test_happy_path_delivers_signed_payload_and_completes_the_document(
    db: DatabaseSessions,
) -> None:
    job_id = await seed(db)
    store = MemoryObjectStore()
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    result = await run_export(db, store, job_id, handler)
    assert result.outcome == "delivered"
    assert result.attempt_number == 1

    job_state, document_state, attempts = await job_and_document_state(db, job_id)
    assert (job_state, document_state, attempts) == ("succeeded", "completed", 1)

    # The receiver got a verifiable signature over the STORED artifact.
    (request,) = received
    assert verify_webhook_signature(
        SECRET, request.content, request.headers[SIGNATURE_HEADER], now=NOW
    )
    async with db.session_scope() as session:
        job = await ExportJobRepository(session, CONTEXT).get(job_id)
        assert job is not None
        (artifact,) = await ArtifactRepository(session, CONTEXT).list_for_document(job.document_id)
        assert await store.get(artifact.object_key) == request.content
        assert request.headers[IDEMPOTENCY_HEADER] == job.business_key


async def test_timeout_then_retry_delivers_the_identical_payload(db: DatabaseSessions) -> None:
    job_id = await seed(db)
    store = MemoryObjectStore()
    bodies: list[bytes] = []
    fail_first = {"pending": True}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if fail_first["pending"]:
            fail_first["pending"] = False
            raise httpx.ConnectTimeout("slow receiver")
        return httpx.Response(200)

    first = await run_export(db, store, job_id, handler)
    assert first.outcome == "retryable_error"
    job_state, document_state, attempts = await job_and_document_state(db, job_id)
    assert (job_state, document_state, attempts) == ("failed_retryable", "exporting", 1)

    second = await run_export(db, store, job_id, handler, timestamp=NOW + 86_400)
    assert second.outcome == "delivered"
    job_state, document_state, attempts = await job_and_document_state(db, job_id)
    assert (job_state, document_state, attempts) == ("succeeded", "completed", 2)
    # The payload is FIXED across retries: byte-identical requests.
    assert bodies[0] == bodies[1]
    async with db.session_scope() as session:
        attempts_rows = await DeliveryAttemptRepository(session, CONTEXT).list_for_job(job_id)
        assert [a.outcome for a in attempts_rows] == ["retryable_error", "delivered"]


async def test_terminal_rejection_fails_job_and_document_then_replay_recovers(
    db: DatabaseSessions,
) -> None:
    job_id = await seed(db)
    store = MemoryObjectStore()

    result = await run_export(db, store, job_id, lambda _r: httpx.Response(400, text="nope"))
    assert result.outcome == "terminal_error"
    job_state, document_state, attempts = await job_and_document_state(db, job_id)
    assert (job_state, document_state, attempts) == ("failed_terminal", "failed_terminal", 1)

    # Replay requires a reason and re-queues the SAME job — same payload,
    # same business key, nothing re-extracted.
    async with db.session_scope() as session:
        job = await ExportJobRepository(session, CONTEXT).get(job_id)
        assert job is not None
        with pytest.raises(ValueError, match="needs a reason"):
            await replay_export_job(session, CONTEXT, job=job, actor_id="user:op", reason="  ")
        await replay_export_job(
            session, CONTEXT, job=job, actor_id="user:op", reason="receiver bug fixed"
        )
        assert job.state == "pending"

    replayed = await run_export(db, store, job_id, lambda _r: httpx.Response(200))
    assert replayed.outcome == "delivered"
    job_state, _document_state, attempts = await job_and_document_state(db, job_id)
    assert (job_state, attempts) == ("succeeded", 2)


async def test_settled_jobs_are_never_redelivered(db: DatabaseSessions) -> None:
    job_id = await seed(db)
    store = MemoryObjectStore()
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200)

    assert (await run_export(db, store, job_id, handler)).outcome == "delivered"
    # Exactly-once: the settled job is a no-op — no request, no attempt.
    again = await run_export(db, store, job_id, handler)
    assert again.outcome == "skipped"
    assert calls["count"] == 1
    _job_state, _document_state, attempts = await job_and_document_state(db, job_id)
    assert attempts == 1


async def test_missing_credential_is_a_named_terminal_failure(db: DatabaseSessions) -> None:
    job_id = await seed(db, with_credential=False)
    store = MemoryObjectStore()
    result = await run_export(db, store, job_id, lambda _r: httpx.Response(200))
    assert result.outcome == "terminal_error"
    assert result.detail is not None and "no live credential" in result.detail
    job_state, document_state, _ = await job_and_document_state(db, job_id)
    assert (job_state, document_state) == ("failed_terminal", "failed_terminal")


@pytest.mark.parametrize(
    "integration_status", [IntegrationStatus.PAUSED, IntegrationStatus.ARCHIVED]
)
async def test_non_active_integrations_never_deliver(
    db: DatabaseSessions, integration_status: IntegrationStatus
) -> None:
    job_id = await seed(db, integration_status=integration_status)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    result = await run_export(db, MemoryObjectStore(), job_id, handler)
    assert result.outcome == "terminal_error"
    assert result.detail is not None and f"integration is {integration_status}" in result.detail
    assert calls == 0


# -- ExportDeliveryHandler: the worker-side export.deliver job handler (EXP-008/010) --


def test_worker_export_job_type_matches_the_api() -> None:
    # The API enqueues under its own EXPORT_JOB_TYPE and the worker
    # registers a handler under the worker's; the two packages never
    # import each other, so this test is what keeps the literal in sync.
    from soa_api.services.export_orchestration import EXPORT_JOB_TYPE as API_EXPORT_JOB_TYPE

    assert EXPORT_JOB_TYPE == API_EXPORT_JOB_TYPE == "export.deliver"


async def test_delivery_handler_processes_a_queue_job_end_to_end(db: DatabaseSessions) -> None:
    job_id = await seed(db)
    store = MemoryObjectStore()
    received: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    async with make_client(receiver) as client:
        handler = ExportDeliveryHandler(
            db, store, client, SECRETS, ALLOWLIST, resolve=public_resolver
        )
        await handler.handle({"organization_id": str(ORG), "export_job_id": str(job_id)})

    assert len(received) == 1
    job_state, document_state, attempts = await job_and_document_state(db, job_id)
    assert (job_state, document_state, attempts) == ("succeeded", "completed", 1)


async def test_delivery_handler_fails_closed_on_an_empty_allowlist(db: DatabaseSessions) -> None:
    # The fail-closed default: with no egress allowlist, the destination
    # is refused before any network call — a clean terminal failure an
    # operator fixes by configuring the allowlist, never an SSRF attempt.
    job_id = await seed(db)
    store = MemoryObjectStore()
    reached: list[httpx.Request] = []

    async with make_client(lambda r: reached.append(r) or httpx.Response(200)) as client:
        handler = ExportDeliveryHandler(db, store, client, SECRETS, (), resolve=public_resolver)
        await handler.handle({"organization_id": str(ORG), "export_job_id": str(job_id)})

    assert reached == []  # nothing left the process
    job_state, document_state, _ = await job_and_document_state(db, job_id)
    assert (job_state, document_state) == ("failed_terminal", "failed_terminal")
