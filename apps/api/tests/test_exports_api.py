"""Delivery history API tests (EXP-009): permission separation, retry
and replay controls (reason required), and secret-free responses."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_config import MemorySecretStore
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.exports import (
    ExportJobRepository,
    ExportJobState,
    create_export_job,
    record_delivery_attempt,
    transition_export_job,
)
from soa_db.integrations import (
    create_integration,
    create_mapping_draft,
    publish_mapping_draft,
    store_integration_credential,
)
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}  # all tenant permissions
AUDITOR = {"X-Dev-User": "user:auditor"}  # integrations.read, NO replay
REVIEWER = {"X-Dev-User": "user:reviewer"}  # no integrations permissions

SECRET = "whsec_history_secret_value"


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/exports-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST), db=db, object_store=MemoryObjectStore()
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    for headers, email, role in (
        (AUDITOR, "auditor@northstar.example", "auditor"),
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
    ):
        client.post("/orgs/northstar/invitations", json={"email": email}, headers=ADMIN)
        accepted = client.post(
            "/invitations/accept", json={"organization_slug": "northstar"}, headers=headers
        )
        assert (
            client.post(
                f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
                json={"role_slug": role},
                headers=ADMIN,
            ).status_code
            == 201
        )
    return client, db


async def seed_export(
    client: TestClient, db: DatabaseSessions, *, state: ExportJobState
) -> tuple[str, str]:
    """An export job (with one recorded attempt) in the given state.
    Returns (export_job_id, document_id)."""
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    document_id = uuid.uuid4()
    async with db.session_scope() as session:
        integration = await create_integration(
            session,
            context,
            name="ERP",
            slug=f"erp-{state.value}",
            integration_type="webhook",
            endpoint_url="https://erp.northstar.example/orders",
            actor_id="user:test",
        )
        await store_integration_credential(
            session,
            context,
            integration=integration,
            kind="webhook_hmac_secret",
            secret=SECRET,
            actor_id="user:test",
            secret_store=MemorySecretStore(),
        )
        draft = await create_mapping_draft(
            session,
            context,
            integration=integration,
            definition={"fields": [{"target": "PoNumber", "source": "identifiers.po_number"}]},
            actor_id="user:test",
        )
        mapping = await publish_mapping_draft(
            session, context, integration=integration, draft=draft, actor_id="user:test"
        )
        job = await create_export_job(
            session,
            context,
            document_id=document_id,
            run_id=uuid.uuid4(),
            canonical_payload_id=uuid.uuid4(),
            integration_id=integration.id,
            mapping_version_id=mapping.id,
            actor_id="system:test",
        )
        if state != ExportJobState.PENDING:
            await transition_export_job(
                session, context, job=job, to_state=ExportJobState.IN_PROGRESS, actor_id="s"
            )
            await record_delivery_attempt(
                session,
                context,
                job=job,
                outcome="retryable_error"
                if state == ExportJobState.FAILED_RETRYABLE
                else "terminal_error",
                response_status=503 if state == ExportJobState.FAILED_RETRYABLE else 400,
                safe_error="receiver returned an error",
                request_sha256="a" * 64,
            )
            await transition_export_job(
                session, context, job=job, to_state=state, actor_id="s", reason="seeded"
            )
        return str(job.id), str(document_id)


async def test_history_reads_are_permission_separated_and_secret_free(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    job_id, document_id = await seed_export(client, db, state=ExportJobState.FAILED_RETRYABLE)

    listing = client.get(f"/orgs/northstar/exports?document_id={document_id}", headers=AUDITOR)
    assert listing.status_code == 200
    (item,) = listing.json()["items"]
    assert item["id"] == job_id
    assert item["state"] == "failed_retryable"
    assert item["integration_slug"] == "erp-failed_retryable"

    detail = client.get(f"/orgs/northstar/exports/{job_id}", headers=AUDITOR)
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["mapping_version_number"] == 1
    (attempt,) = payload["attempts"]
    assert attempt["outcome"] == "retryable_error"
    assert attempt["response_status"] == 503
    assert attempt["request_sha256"] == "a" * 64

    # No secret material, no signature headers, anywhere.
    for response in (listing, detail):
        assert SECRET not in response.text
        assert "whsec" not in response.text
        assert "X-SOA-Signature" not in response.text

    # No integrations permission at all: the history is invisible.
    assert client.get("/orgs/northstar/exports", headers=REVIEWER).status_code == 403


async def test_retry_and_replay_controls(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, db = harness
    retryable_id, _ = await seed_export(client, db, state=ExportJobState.FAILED_RETRYABLE)
    terminal_id, _ = await seed_export(client, db, state=ExportJobState.FAILED_TERMINAL)

    # Read-only roles cannot fire deliveries (permission separation).
    assert (
        client.post(f"/orgs/northstar/exports/{retryable_id}/retry", headers=AUDITOR).status_code
        == 403
    )
    assert (
        client.post(
            f"/orgs/northstar/exports/{terminal_id}/replay",
            json={"reason": "x"},
            headers=AUDITOR,
        ).status_code
        == 403
    )

    # Retry: only retryable failures; re-queues the same job.
    wrong_state = client.post(f"/orgs/northstar/exports/{terminal_id}/retry", headers=ADMIN)
    assert wrong_state.status_code == 409
    retried = client.post(f"/orgs/northstar/exports/{retryable_id}/retry", headers=ADMIN)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "pending"
    assert retried.json()["attempt_count"] == 1  # history intact

    # Replay: settled failures only, reason REQUIRED.
    blank = client.post(
        f"/orgs/northstar/exports/{terminal_id}/replay", json={"reason": "  "}, headers=ADMIN
    )
    assert blank.status_code in (400, 422)
    replayed = client.post(
        f"/orgs/northstar/exports/{terminal_id}/replay",
        json={"reason": "receiver contract fixed"},
        headers=ADMIN,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["state"] == "pending"
    assert replayed.json()["business_key"]  # same key, stated in the row

    # Both controls staged fresh queue jobs.
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        queue_jobs = (
            (await session.execute(select(Job).where(Job.job_type == "export.deliver")))
            .scalars()
            .all()
        )
        assert len(queue_jobs) == 2
        # And the export jobs themselves are back in the queue-ready state.
        for export_id in (retryable_id, terminal_id):
            job = await ExportJobRepository(session, context).get(uuid.UUID(export_id))
            assert job is not None and job.state == "pending"

    # Cancel needs manage; a pending job cancels with a reason.
    cancelled = client.post(
        f"/orgs/northstar/exports/{retryable_id}/cancel",
        json={"reason": "customer withdrew the order"},
        headers=ADMIN,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert (
        client.post(
            f"/orgs/northstar/exports/{terminal_id}/cancel",
            json={"reason": "x"},
            headers=AUDITOR,
        ).status_code
        == 403
    )
