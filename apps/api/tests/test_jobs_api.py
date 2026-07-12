"""Job administration API tests (JOB-006).

Covers tenant scoping (own jobs only — system jobs and other tenants'
jobs are invisible), the replay/cancel state rules, required + audited
reasons, permission separation (jobs.read vs jobs.manage), and the
non-grantability of the internal jobs.admin permission.
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.jobs import JobStatus, enqueue_job

ADMIN = {"X-Dev-User": "user:reviewer"}  # creator -> org-admin
MEMBER = {"X-Dev-User": "user:supervisor"}  # invited -> roles assigned per test


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/jobs-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(ApiSettings(environment=Environment.TEST), db=db)
    return TestClient(app, raise_server_exceptions=False), db


def create_org(client: TestClient, slug: str = "northstar") -> uuid.UUID:
    response = client.post(
        "/organizations", json={"name": "Northstar", "slug": slug}, headers=ADMIN
    )
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


async def seed_job(
    db: DatabaseSessions,
    organization_id: uuid.UUID | None,
    *,
    job_type: str = "document.extract",
    status: JobStatus = JobStatus.PENDING,
    attempts: int = 0,
) -> uuid.UUID:
    async with db.session_scope() as session:
        job = await enqueue_job(
            session,
            job_type=job_type,
            payload={"document": "SECRET-CONTENTS"},
            organization_id=organization_id,
        )
        if status is not JobStatus.PENDING:
            job.status = JobStatus.RUNNING
            if status is not JobStatus.RUNNING:
                job.status = status
        job.attempts = attempts
        if status is JobStatus.DEAD_LETTER:
            job.last_error = "upstream timeout"
        await session.flush()
        return job.id


async def test_listing_shows_only_this_tenants_jobs(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = create_org(client)
    other_org_id = uuid.uuid4()
    await seed_job(db, org_id, job_type="mine.a")
    await seed_job(db, other_org_id, job_type="theirs.b")
    await seed_job(db, None, job_type="system.cleanup")

    response = client.get("/orgs/northstar/jobs", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert [item["job_type"] for item in body["items"]] == ["mine.a"]
    # Payloads are never part of the admin surface.
    assert "payload" not in body["items"][0]
    assert "SECRET" not in response.text


async def test_foreign_job_detail_is_not_found(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    create_org(client)
    foreign_id = await seed_job(db, uuid.uuid4(), job_type="theirs.b")
    system_id = await seed_job(db, None, job_type="system.cleanup")
    assert client.get(f"/orgs/northstar/jobs/{foreign_id}", headers=ADMIN).status_code == 404
    assert client.get(f"/orgs/northstar/jobs/{system_id}", headers=ADMIN).status_code == 404


async def test_status_filter(harness: tuple[TestClient, DatabaseSessions]) -> None:
    client, db = harness
    org_id = create_org(client)
    await seed_job(db, org_id, job_type="a", status=JobStatus.PENDING)
    await seed_job(db, org_id, job_type="b", status=JobStatus.DEAD_LETTER, attempts=3)
    response = client.get("/orgs/northstar/jobs?job_status=dead_letter", headers=ADMIN)
    assert [item["job_type"] for item in response.json()["items"]] == ["b"]


async def test_replay_requires_reason_and_writes_audit(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = create_org(client)
    job_id = await seed_job(db, org_id, status=JobStatus.DEAD_LETTER, attempts=5)

    no_reason = client.post(f"/orgs/northstar/jobs/{job_id}/replay", json={}, headers=ADMIN)
    assert no_reason.status_code == 422

    response = client.post(
        f"/orgs/northstar/jobs/{job_id}/replay",
        json={"reason": "provider outage resolved"},
        headers=ADMIN,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0, "replay grants a fresh attempt budget"

    async with db.session_scope() as session:
        event = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == "job.replayed"))
        ).scalar_one()
        assert event.target_id == str(job_id)
        assert event.summary["reason"] == "provider outage resolved"
        assert event.summary["previous_attempts"] == 5


async def test_replay_of_non_dead_letter_conflicts(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = create_org(client)
    job_id = await seed_job(db, org_id, status=JobStatus.PENDING)
    response = client.post(
        f"/orgs/northstar/jobs/{job_id}/replay", json={"reason": "why not"}, headers=ADMIN
    )
    assert response.status_code == 409


async def test_cancel_pending_job_is_audited_but_running_conflicts(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = create_org(client)
    pending_id = await seed_job(db, org_id, status=JobStatus.PENDING)
    running_id = await seed_job(db, org_id, status=JobStatus.RUNNING, attempts=1)

    cancelled = client.post(
        f"/orgs/northstar/jobs/{pending_id}/cancel",
        json={"reason": "duplicate upload"},
        headers=ADMIN,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    running = client.post(
        f"/orgs/northstar/jobs/{running_id}/cancel",
        json={"reason": "too slow"},
        headers=ADMIN,
    )
    assert running.status_code == 409

    async with db.session_scope() as session:
        event = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == "job.cancelled"))
        ).scalar_one()
        assert event.summary["reason"] == "duplicate upload"


async def test_jobs_read_without_jobs_manage_cannot_mutate(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = create_org(client)
    job_id = await seed_job(db, org_id, status=JobStatus.DEAD_LETTER, attempts=1)

    client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=ADMIN,
    )
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=MEMBER
    )
    membership_id = accepted.json()["membership_id"]
    # auditor holds jobs.read but not jobs.manage.
    assign = client.post(
        f"/orgs/northstar/members/{membership_id}/roles",
        json={"role_slug": "auditor"},
        headers=ADMIN,
    )
    assert assign.status_code == 201

    assert client.get("/orgs/northstar/jobs", headers=MEMBER).status_code == 200
    denied = client.post(
        f"/orgs/northstar/jobs/{job_id}/replay", json={"reason": "curious"}, headers=MEMBER
    )
    assert denied.status_code == 403


async def test_internal_jobs_admin_permission_is_not_tenant_grantable(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _ = harness
    create_org(client)
    response = client.post(
        "/orgs/northstar/roles",
        json={"name": "Platform Ops", "slug": "platform-ops", "permissions": ["jobs.admin"]},
        headers=ADMIN,
    )
    assert response.status_code == 422, "internal permissions must fail closed for tenants"

    # And the org-admin template itself must not include it.
    roles = client.get("/orgs/northstar/roles", headers=ADMIN).json()
    org_admin = next(role for role in roles if role["slug"] == "org-admin")
    assert "jobs.admin" not in org_admin["permissions"]
    assert "jobs.read" in org_admin["permissions"]
    assert "jobs.manage" in org_admin["permissions"]


async def test_queue_stats_counts_only_this_tenant(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    org_id = create_org(client)
    await seed_job(db, org_id, job_type="a", status=JobStatus.PENDING)
    await seed_job(db, org_id, job_type="b", status=JobStatus.PENDING)
    await seed_job(db, org_id, job_type="c", status=JobStatus.DEAD_LETTER, attempts=2)
    await seed_job(db, uuid.uuid4(), job_type="theirs", status=JobStatus.PENDING)

    response = client.get("/orgs/northstar/jobs/stats", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["by_status"]["pending"] == 2
    assert body["by_status"]["dead_letter"] == 1
    assert body["by_status"]["running"] == 0
    assert body["oldest_pending_run_after"] is not None
