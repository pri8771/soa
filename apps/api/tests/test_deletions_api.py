"""Authenticated API for approval-gated erasure and legal holds."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.deletion_requests import (
    DOCUMENT_DELETION_JOB_TYPE,
    DeletionRequestRepository,
    DeletionRequestState,
)
from soa_db.documents import (
    DocumentRepository,
    DocumentState,
    SourceChannel,
    create_document,
    transition_document,
)
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}
APPROVER = {"X-Dev-User": "user:supervisor"}
APPROVE_ONLY = {"X-Dev-User": "user:integration-admin"}
REVIEWER = {"X-Dev-User": "user:reviewer"}
OUTSIDER = APPROVER


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/deletions-api.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    client = TestClient(
        create_app(
            ApiSettings(environment=Environment.TEST),
            db=db,
            object_store=MemoryObjectStore(),
        ),
        raise_server_exceptions=False,
    )
    assert (
        client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/orgs/northstar/roles",
            json={
                "name": "Deletion approver",
                "slug": "deletion-approver",
                "permissions": ["data.delete.approve"],
            },
            headers=ADMIN,
        ).status_code
        == 201
    )
    for headers, email, role in (
        (APPROVER, "supervisor@northstar.example", "org-admin"),
        (APPROVE_ONLY, "integrations@northstar.example", "deletion-approver"),
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
    ):
        assert (
            client.post(
                "/orgs/northstar/invitations", json={"email": email}, headers=ADMIN
            ).status_code
            == 201
        )
        accepted = client.post(
            "/invitations/accept",
            json={"organization_slug": "northstar"},
            headers=headers,
        )
        assert accepted.status_code == 200
        assert (
            client.post(
                f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
                json={"role_slug": role},
                headers=ADMIN,
            ).status_code
            == 201
        )
    return client, db


async def _settled_document(client: TestClient, db: DatabaseSessions) -> uuid.UUID:
    organization_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="order.pdf",
            content_sha256="a" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
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
        return document.id


async def test_request_and_approval_are_permissioned_and_two_person(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id = await _settled_document(client, db)
    path = f"/orgs/northstar/documents/{document_id}/deletion-requests"
    status_path = f"/orgs/northstar/documents/{document_id}/deletion-request"

    assert (
        client.post(path, json={"reason": "verified request"}, headers=REVIEWER).status_code == 403
    )
    assert (
        client.post(path, json={"reason": "verified request"}, headers=APPROVE_ONLY).status_code
        == 403
    )
    assert client.get(status_path, headers=REVIEWER).status_code == 403
    assert client.get(status_path, headers=APPROVE_ONLY).status_code == 404
    created = client.post(path, json={"reason": "verified request"}, headers=ADMIN)
    assert created.status_code == 202, created.text
    request_id = created.json()["id"]
    status_response = client.get(status_path, headers=APPROVE_ONLY)
    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["id"] == request_id
    approver_list = client.get("/orgs/northstar/deletion-requests", headers=APPROVE_ONLY)
    assert approver_list.status_code == 200, approver_list.text
    assert [item["id"] for item in approver_list.json()["items"]] == [request_id]
    copied_export = client.post(
        f"/orgs/northstar/documents/{document_id}/data-exports", headers=ADMIN
    )
    assert copied_export.status_code == 410
    approve_path = f"/orgs/northstar/deletion-requests/{request_id}/approve"
    self_approval = client.post(approve_path, json={"reason": "scope verified"}, headers=ADMIN)
    assert self_approval.status_code == 409
    assert "cannot approve" in self_approval.text

    approved = client.post(
        approve_path,
        json={"reason": "authority and scope independently verified"},
        headers=APPROVE_ONLY,
    )
    assert approved.status_code == 202, approved.text
    assert approved.json()["state"] == DeletionRequestState.APPROVED.value
    listed = client.get("/orgs/northstar/deletion-requests", headers=ADMIN)
    assert [item["id"] for item in listed.json()["items"]] == [request_id]

    async with db.session_scope() as session:
        jobs = (
            (await session.execute(select(Job).where(Job.job_type == DOCUMENT_DELETION_JOB_TYPE)))
            .scalars()
            .all()
        )
        assert len(jobs) == 1
        assert jobs[0].payload["deletion_request_id"] == request_id


async def test_legal_hold_blocks_and_release_does_not_auto_approve(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id = await _settled_document(client, db)
    requested = client.post(
        f"/orgs/northstar/documents/{document_id}/deletion-requests",
        json={"reason": "verified request"},
        headers=ADMIN,
    )
    request_id = requested.json()["id"]
    hold = client.post(
        f"/orgs/northstar/documents/{document_id}/legal-holds",
        json={"reason": "preservation notice"},
        headers=APPROVER,
    )
    assert hold.status_code == 201, hold.text
    blocked = client.post(
        f"/orgs/northstar/deletion-requests/{request_id}/approve",
        json={"reason": "authority verified"},
        headers=APPROVER,
    )
    assert blocked.status_code == 409
    released = client.post(
        f"/orgs/northstar/legal-holds/{hold.json()['id']}/release",
        json={"reason": "notice withdrawn"},
        headers=APPROVER,
    )
    assert released.status_code == 200
    assert released.json()["state"] == "released"
    current = client.get("/orgs/northstar/deletion-requests", headers=ADMIN).json()["items"][0]
    assert current["state"] == DeletionRequestState.PENDING_APPROVAL.value


async def test_cancelled_request_restores_export_and_can_be_reopened(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id = await _settled_document(client, db)
    path = f"/orgs/northstar/documents/{document_id}/deletion-requests"
    request = client.post(path, json={"reason": "incorrect initial scope"}, headers=ADMIN)
    request_id = request.json()["id"]
    cancelled = client.post(
        f"/orgs/northstar/deletion-requests/{request_id}/cancel",
        json={"reason": "request was recorded for the wrong document"},
        headers=ADMIN,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == DeletionRequestState.CANCELLED.value
    assert cancelled.json()["cancelled_at"]

    exported = client.post(f"/orgs/northstar/documents/{document_id}/data-exports", headers=ADMIN)
    assert exported.status_code == 201, exported.text
    reopened = client.post(path, json={"reason": "corrected verified scope"}, headers=ADMIN)
    assert reopened.status_code == 202
    assert reopened.json()["id"] == request_id
    assert reopened.json()["state"] == DeletionRequestState.PENDING_APPROVAL.value
    assert reopened.json()["cancelled_at"] is None


# -- cross-tenant isolation (AGENTS.md §6) ---------------------------------------------


async def seed_other_org_document(client: TestClient, db: DatabaseSessions) -> str:
    """A second organization ('southwind', org B) with one deletable
    document, created by an unrelated admin — used to prove an org-A caller
    cannot reach it."""
    for path, body in (
        ("/organizations", {"name": "Southwind", "slug": "southwind"}),
        ("/orgs/southwind/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/southwind/processes/purchase-orders/streams",
            {"name": "Uploads", "slug": "uploads"},
        ),
    ):
        assert client.post(path, json=body, headers=OUTSIDER).status_code == 201
    org_id = uuid.UUID(client.get("/orgs/southwind", headers=OUTSIDER).json()["id"])
    stream_id = uuid.UUID(client.get("/orgs/southwind/streams", headers=OUTSIDER).json()[0]["id"])
    context = OrganizationContext(organization_id=org_id)
    data = b"%PDF-1.7 org-B document bytes"
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=stream_id,
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="b" * 64,
            size_bytes=len(data),
            content_type="application/pdf",
            actor_id="user:test",
        )
        # received -> cancelled is a single valid transition into a
        # deletable state, so the document is genuinely erasable — the only
        # thing stopping deletion here is tenant scope, not its state.
        await transition_document(
            session,
            context,
            document=document,
            to_state=DocumentState.CANCELLED,
            actor_id="worker",
        )
        return str(document.id)


async def test_deletion_cannot_cross_tenants(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    """An org admin can request deletion only for its own tenant's documents.

    The target
    document lives in org B (southwind). The tenant-scoped lookup makes it
    indistinguishable from a nonexistent document: the caller's OWN org
    path answers 404 — never 403 (which would confirm the id exists) or
    500. The org-B document must survive untouched."""
    client, db = harness
    foreign_id = await seed_other_org_document(client, db)

    response = client.post(
        f"/orgs/northstar/documents/{foreign_id}/deletion-requests",
        json={"reason": "cross-tenant probe"},
        headers=ADMIN,
    )
    assert response.status_code == 404

    # Org B's document is untouched: still present, still cancelled, and no
    # deletion request was ever minted for it.
    org_b = uuid.UUID(client.get("/orgs/southwind", headers=OUTSIDER).json()["id"])
    context = OrganizationContext(organization_id=org_b)
    async with db.session_scope() as session:
        document = await DocumentRepository(session, context).get(uuid.UUID(foreign_id))
        assert document is not None
        assert document.state == DocumentState.CANCELLED.value
        assert (
            await DeletionRequestRepository(session, context).get_for_document(
                uuid.UUID(foreign_id)
            )
            is None
        )


# -- rate limiting (SEC-003) ----------------------------------------------------------


async def test_deletion_requests_are_capped_per_principal(tmp_path: Path) -> None:
    """The deletion-request endpoint is rate limited per principal, the same abuse
    control every other mutation carries. Like reprocess, the limiter runs
    BEFORE the document lookup, so a second probe answers 429 even though
    both would otherwise be 404 — abuse probing is throttled too."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/deletion-ratelimit.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST, rate_limit_deletions_per_minute=1),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            "/organizations",
            json={"name": "Northstar", "slug": "northstar"},
            headers=ADMIN,
        ).status_code
        == 201
    )

    probe = "019f0000-0000-7000-8000-000000000000"
    path = f"/orgs/northstar/documents/{probe}/deletion-requests"
    assert client.post(path, json={"reason": "first probe"}, headers=ADMIN).status_code == 404
    limited = client.post(path, json={"reason": "second probe"}, headers=ADMIN)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-RateLimit-Remaining"] == "0"


# -- concurrency: StaleDataError -> 409 -----------------------------------------------


def test_stale_data_error_maps_to_409_conflict() -> None:
    """A concurrent modification during deletion surfaces as 409, not 500.

    Honest scope: this exercises the errors.py handler mapping directly,
    which is what the deletion endpoint (and every other read-then-flush on
    a VersionedMixin row — approvals, cancel, reprocess) relies on. A fully
    deterministic end-to-end race THROUGH the endpoint is NOT reproducible
    with the synchronous TestClient: the whole request runs as one blocking
    call, so a second session cannot bump the document's version in the
    window between the endpoint's load and its flush. What is proven here is
    exactly the contract the endpoint depends on — if the ORM flush loses
    the optimistic-concurrency race and raises StaleDataError, the client
    sees a 409 conflict with the safe envelope, never a 500 and never the
    raw SQLAlchemy message. It does NOT prove the endpoint itself flushes
    under a real concurrent writer; that remains an integration concern the
    sync harness cannot force."""
    from fastapi import FastAPI
    from sqlalchemy.orm.exc import StaleDataError

    from soa_api.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app, ApiSettings(environment=Environment.TEST))

    @app.post("/boom")
    async def boom() -> None:
        raise StaleDataError("UPDATE documents ... matched 0 rows; concurrent writer won")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/boom")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "conflict"
    assert "concurrently" in body["error"]["message"]
    # The raw SQLAlchemy detail never reaches the client.
    assert "matched 0 rows" not in response.text
