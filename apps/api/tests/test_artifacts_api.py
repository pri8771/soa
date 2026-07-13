"""Signed download authorization tests (STO-004): tenant scope, expiry,
permission boundaries, no support bypass."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import ArtifactKind, create_artifact
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore, SignedUrlExpiredError

ADMIN = {"X-Dev-User": "user:reviewer"}
OUTSIDER = {"X-Dev-User": "user:supervisor"}
DOC = uuid.UUID("44444444-4444-4444-8444-444444444444")


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/artifacts-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(ApiSettings(environment=Environment.TEST), db=db, object_store=store)
    return TestClient(app, raise_server_exceptions=False), db, store


async def seed_artifact(
    client: TestClient, db: DatabaseSessions, store: MemoryObjectStore, slug: str = "northstar"
) -> str:
    org_id = uuid.UUID(client.get(f"/orgs/{slug}", headers=ADMIN).json()["id"])
    key = f"orgs/{org_id}/documents/{DOC}/original/abc123-po.pdf"
    metadata = await store.put(key, b"pdf bytes", content_type="application/pdf")
    async with db.session_scope() as session:
        artifact = await create_artifact(
            session,
            OrganizationContext(organization_id=org_id),
            document_id=DOC,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=metadata.sha256,
            size_bytes=metadata.size,
            content_type="application/pdf",
        )
        return str(artifact.id)


def make_org(client: TestClient, slug: str = "northstar", headers: dict[str, str] = ADMIN) -> None:
    response = client.post(
        "/organizations", json={"name": slug.title(), "slug": slug}, headers=headers
    )
    assert response.status_code == 201, response.text


async def test_download_url_is_issued_scoped_and_audited(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    make_org(client)
    artifact_id = await seed_artifact(client, db, store)

    listed = client.get(f"/orgs/northstar/documents/{DOC}/artifacts", headers=ADMIN)
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert row["kind"] == "original"
    assert "object_key" not in row, "object keys never leave the server"

    issued = client.post(f"/orgs/northstar/artifacts/{artifact_id}/download-url", headers=ADMIN)
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["method"] == "GET"
    # The URL resolves to exactly the artifact's object.
    key = store.verify_signed_url(body["url"])
    assert f"documents/{DOC}" in key

    # Expiry honors settings (default 300s) and the URL dies afterwards.
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert expires_at <= datetime.now(tz=UTC) + timedelta(seconds=300)
    with pytest.raises(SignedUrlExpiredError):
        store.verify_signed_url(body["url"], now=expires_at + timedelta(seconds=1))

    # Issuance is audited.
    from sqlalchemy import select

    from soa_db.audit import AuditEvent

    async with db.session_scope() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "artifact.download_url_issued")
                )
            )
            .scalars()
            .all()
        )
        assert [e.target_id for e in events] == [artifact_id]


async def test_cross_tenant_and_no_support_bypass(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    make_org(client)
    artifact_id = await seed_artifact(client, db, store)

    # No membership at all: the organization itself must not leak.
    denied = client.post(f"/orgs/northstar/artifacts/{artifact_id}/download-url", headers=OUTSIDER)
    assert denied.status_code == 404

    # A full admin of a DIFFERENT organization cannot reach the artifact
    # through their own org path — the id is useless across tenants. This
    # is also the support-access policy: there is no staff role that skips
    # membership; support reads require an explicit membership in the
    # customer's organization.
    make_org(client, slug="other-org", headers=OUTSIDER)
    cross = client.post(f"/orgs/other-org/artifacts/{artifact_id}/download-url", headers=OUTSIDER)
    assert cross.status_code == 404


async def test_reads_require_documents_read_permission(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    make_org(client)
    artifact_id = await seed_artifact(client, db, store)
    # Invite the outsider and grant configurator (no documents.read).
    client.post(
        "/orgs/northstar/invitations",
        json={"email": "supervisor@northstar.example"},
        headers=ADMIN,
    )
    accepted = client.post(
        "/invitations/accept", json={"organization_slug": "northstar"}, headers=OUTSIDER
    )
    membership_id = accepted.json()["membership_id"]
    # Every system role template includes documents.read, so build a
    # custom role without it.
    created_role = client.post(
        "/orgs/northstar/roles",
        json={
            "name": "Config only",
            "slug": "config-only",
            "permissions": ["organization.read", "processes.read"],
        },
        headers=ADMIN,
    )
    assert created_role.status_code == 201, created_role.text
    granted = client.post(
        f"/orgs/northstar/members/{membership_id}/roles",
        json={"role_slug": "config-only"},
        headers=ADMIN,
    )
    assert granted.status_code == 201, granted.text

    denied = client.post(f"/orgs/northstar/artifacts/{artifact_id}/download-url", headers=OUTSIDER)
    assert denied.status_code == 403, denied.text


async def test_missing_object_is_a_conflict_not_a_dead_url(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    make_org(client)
    artifact_id = await seed_artifact(client, db, store)
    # Simulate object loss (bucket-side deletion outside the service API).
    org_id = client.get("/orgs/northstar", headers=ADMIN).json()["id"]
    await store.delete(f"orgs/{org_id}/documents/{DOC}/original/abc123-po.pdf")

    response = client.post(f"/orgs/northstar/artifacts/{artifact_id}/download-url", headers=ADMIN)
    assert response.status_code == 409
    assert "missing" in response.text
