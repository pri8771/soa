"""Authenticated API for approval-gated erasure and legal holds."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.deletion_requests import DOCUMENT_DELETION_JOB_TYPE, DeletionRequestState
from soa_db.documents import DocumentState, SourceChannel, create_document, transition_document
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}
APPROVER = {"X-Dev-User": "user:supervisor"}
APPROVE_ONLY = {"X-Dev-User": "user:integration-admin"}
REVIEWER = {"X-Dev-User": "user:reviewer"}


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
