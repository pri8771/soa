"""Reprocess API tests (PRC-013): the state matrix (who may re-enter the
queue, what is protected by policy), the permission matrix (reprocess vs
config authority), and the pinned-configuration modes."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import DocumentState, SourceChannel, create_document, transition_document
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_db.runs import start_run
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}  # org creator: org-admin (all permissions)
SUPERVISOR = {"X-Dev-User": "user:supervisor"}  # supervisor: reprocess, no streams.manage
REVIEWER = {"X-Dev-User": "user:reviewer"}  # reviewer: no documents.reprocess

#: received -> ... -> the named state, along the happy chain.
_CHAIN = [
    DocumentState.VALIDATING_FILE,
    DocumentState.QUEUED,
    DocumentState.PREPROCESSING,
    DocumentState.CLASSIFYING,
    DocumentState.SPLITTING,
    DocumentState.EXTRACTING,
    DocumentState.NORMALIZING,
    DocumentState.VALIDATING_DATA,
]


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/reprocess.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST), db=db, object_store=MemoryObjectStore()
    )
    client = TestClient(app, raise_server_exceptions=False)
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Uploads", "slug": "uploads"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    for headers, email, role in (
        (SUPERVISOR, "supervisor@northstar.example", "supervisor"),
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
    ):
        client.post("/orgs/northstar/invitations", json={"email": email}, headers=ADMIN)
        accepted = client.post(
            "/invitations/accept", json={"organization_slug": "northstar"}, headers=headers
        )
        membership_id = accepted.json()["membership_id"]
        granted = client.post(
            f"/orgs/northstar/members/{membership_id}/roles",
            json={"role_slug": role},
            headers=ADMIN,
        )
        assert granted.status_code == 201, granted.text
    return client, db


async def seed_document(
    client: TestClient,
    db: DatabaseSessions,
    *,
    to_state: DocumentState | None,
    with_run: bool = False,
) -> tuple[str, str | None]:
    """A document walked to ``to_state`` (None = leave at received);
    optionally with a finished-config run pinned to fingerprint 'a'*64."""
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/northstar/streams", headers=ADMIN).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    run_id: str | None = None
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="c" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        if to_state is not None:
            tail = {
                DocumentState.APPROVED: [DocumentState.APPROVED],
                DocumentState.EXPORTING: [DocumentState.APPROVED, DocumentState.EXPORTING],
                DocumentState.COMPLETED: [
                    DocumentState.APPROVED,
                    DocumentState.EXPORTING,
                    DocumentState.COMPLETED,
                ],
            }.get(to_state, [to_state])
            walk = _CHAIN if to_state not in _CHAIN else _CHAIN[: _CHAIN.index(to_state) + 1]
            steps = walk + tail if to_state not in _CHAIN else walk
            for state in steps:
                await transition_document(
                    session, context, document=document, to_state=state, actor_id="worker"
                )
        if with_run:
            run = await start_run(
                session,
                context,
                document_id=document.id,
                input_sha256="c" * 64,
                stream_version_id=None,
                config_fingerprint="a" * 64,
                triggered_by="system:test",
            )
            run_id = str(run.id)
        return str(document.id), run_id


def reprocess(
    client: TestClient, document_id: str, headers: dict[str, str], **body: object
) -> object:
    payload = {"reason": "corrected the schema", "mode": "current_config", **body}
    return client.post(
        f"/orgs/northstar/documents/{document_id}/reprocess", json=payload, headers=headers
    )


# -- state matrix -----------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [DocumentState.REVIEW_REQUIRED, DocumentState.FAILED_TERMINAL, DocumentState.FAILED_RETRYABLE],
)
async def test_reprocessable_states_requeue_with_a_new_run_intent(
    harness: tuple[TestClient, DatabaseSessions], state: DocumentState
) -> None:
    client, db = harness
    document_id, _ = await seed_document(client, db, to_state=state)
    response = reprocess(client, document_id, ADMIN)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state"] == "queued"
    assert payload["run_number"] == 1
    assert "re-execute the full pipeline" in payload["consequence"]
    assert "remain unchanged as evidence" in payload["consequence"]
    async with db.session_scope() as session:
        job = (
            (
                await session.execute(
                    select(Job).where(Job.dedupe_key == f"document.preprocess:{document_id}:run:1")
                )
            )
            .scalars()
            .one()
        )
        assert job.payload["document_id"] == document_id


@pytest.mark.parametrize(
    ("state", "expected_detail"),
    [
        (DocumentState.APPROVED, "protected by policy"),
        (DocumentState.EXPORTING, "protected by policy"),
        (DocumentState.COMPLETED, "protected by policy"),
    ],
)
async def test_business_commitments_are_protected_by_policy(
    harness: tuple[TestClient, DatabaseSessions], state: DocumentState, expected_detail: str
) -> None:
    client, db = harness
    document_id, _ = await seed_document(client, db, to_state=state)
    response = reprocess(client, document_id, ADMIN)
    assert response.status_code == 409
    assert expected_detail in response.json()["error"]["message"]


async def test_active_documents_answer_409_from_the_state_machine(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, _ = await seed_document(client, db, to_state=None)  # still "received"
    response = reprocess(client, document_id, ADMIN)
    assert response.status_code == 409
    assert "not allowed" in response.json()["error"]["message"]


async def test_unknown_document_is_404(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, _db = harness
    response = reprocess(client, str(uuid.uuid4()), ADMIN)
    assert response.status_code == 404


# -- modes ------------------------------------------------------------------------


async def test_retry_mode_pins_the_last_runs_configuration(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, _ = await seed_document(
        client, db, to_state=DocumentState.FAILED_RETRYABLE, with_run=True
    )
    response = reprocess(client, document_id, ADMIN, mode="retry")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["pinned"]["config_fingerprint"] == "a" * 64
    assert payload["run_number"] == 2
    assert "same configuration" in payload["consequence"]


async def test_retry_without_any_run_is_409(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, _ = await seed_document(client, db, to_state=DocumentState.FAILED_RETRYABLE)
    response = reprocess(client, document_id, ADMIN, mode="retry")
    assert response.status_code == 409
    assert "never run" in response.json()["error"]["message"]


async def test_historical_config_mode_pins_the_chosen_run(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, run_id = await seed_document(
        client, db, to_state=DocumentState.REVIEW_REQUIRED, with_run=True
    )
    missing = reprocess(client, document_id, ADMIN, mode="historical_config")
    assert missing.status_code == 400
    unknown = reprocess(
        client, document_id, ADMIN, mode="historical_config", run_id=str(uuid.uuid4())
    )
    assert unknown.status_code == 404
    response = reprocess(client, document_id, ADMIN, mode="historical_config", run_id=run_id)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["pinned"]["config_fingerprint"] == "a" * 64
    assert "HISTORICAL" in payload["consequence"]


# -- permission matrix ---------------------------------------------------------------


async def test_supervisor_may_reprocess_but_not_under_historical_config(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, run_id = await seed_document(
        client, db, to_state=DocumentState.REVIEW_REQUIRED, with_run=True
    )
    denied = reprocess(client, document_id, SUPERVISOR, mode="historical_config", run_id=run_id)
    assert denied.status_code == 403
    assert "streams.manage" in denied.json()["error"]["message"]
    allowed = reprocess(client, document_id, SUPERVISOR, mode="current_config")
    assert allowed.status_code == 200, allowed.text


async def test_reviewer_lacks_the_reprocess_permission_entirely(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, _ = await seed_document(client, db, to_state=DocumentState.REVIEW_REQUIRED)
    response = reprocess(client, document_id, REVIEWER)
    assert response.status_code == 403


async def test_audit_records_the_request_with_mode_and_pins(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id, _ = await seed_document(client, db, to_state=DocumentState.REVIEW_REQUIRED)
    assert reprocess(client, document_id, ADMIN).status_code == 200
    detail = client.get(f"/orgs/northstar/documents/{document_id}", headers=ADMIN).json()
    actions = [entry["action"] for entry in detail["timeline"]]
    assert "document.reprocess_requested" in actions
    entry = next(e for e in detail["timeline"] if e["action"] == "document.reprocess_requested")
    assert entry["summary"]["mode"] == "current_config"
    assert entry["summary"]["run_number"] == 1
