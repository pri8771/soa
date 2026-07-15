"""Customer data export tests (SEC-009): a scoped, signed, expiring
bundle documenting every data category; authorization (data.export) and
counts-only audit; tenant confinement."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.data_export import EXPORT_CATEGORIES
from soa_db.data_export_jobs import DataExportJob
from soa_db.documents import SourceChannel, create_document
from soa_db.extracted_fields import create_extracted_field
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_db.runs import start_run
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:admin"}
AUDITOR = {"X-Dev-User": "user:auditor"}  # has data.export
REVIEWER = {"X-Dev-User": "user:reviewer"}  # NO data.export


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/data-export.db")
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


async def seed_document_with_data(client: TestClient, db: DatabaseSessions) -> uuid.UUID:
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    context = OrganizationContext(organization_id=org_id)
    async with db.session_scope() as session:
        document = await create_document(
            session,
            context,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="a" * 64,
            size_bytes=100,
            content_type="application/pdf",
            actor_id="user:test",
        )
        run = await start_run(
            session,
            context,
            document_id=document.id,
            input_sha256="a" * 64,
            stream_version_id=None,
            config_fingerprint="f" * 64,
            triggered_by="user:test",
        )
        await create_extracted_field(
            session,
            context,
            document_id=document.id,
            run_id=run.id,
            field_key="po_number",
            raw_value="PO-100042",
            confidence=0.98,
            provider="mock",
        )
        return document.id


async def test_export_bundle_documents_all_categories_and_is_signed(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id = await seed_document_with_data(client, db)

    response = client.post(f"/orgs/northstar/documents/{document_id}/data-exports", headers=AUDITOR)
    assert response.status_code == 201, response.text
    body = response.json()

    # Every documented category appears in the manifest and as a file,
    # including the empty ones — the export states what exists honestly.
    category_keys = {c.key for c in EXPORT_CATEGORIES}
    assert set(body["counts"]) == category_keys
    assert {f["category"] for f in body["files"]} == category_keys
    # The seeded categories carry records; a clearly-empty one is zero.
    assert body["counts"]["document"] == 1
    assert body["counts"]["processing_runs"] == 1
    assert body["counts"]["extracted_fields"] == 1
    assert body["counts"]["delivery_attempts"] == 0
    assert body["total_records"] >= 3

    # Every file is signed with an expiry and a content hash; the
    # manifest itself is signed and expiring.
    for entry in body["files"]:
        assert entry["download_url"]
        assert entry["expires_at"]
        assert len(entry["sha256"]) == 64
    assert body["manifest_download_url"]
    assert body["manifest_expires_at"]

    # No extracted VALUE leaks into the API response envelope — content
    # lives only inside the signed bundle files.
    assert "PO-100042" not in response.text


async def test_export_is_audited_counts_only(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id = await seed_document_with_data(client, db)
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])

    assert (
        client.post(
            f"/orgs/northstar/documents/{document_id}/data-exports", headers=AUDITOR
        ).status_code
        == 201
    )
    async with db.session_scope() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.organization_id == org_id,
                        AuditEvent.action == "document.data_export_created",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    summary = events[0].summary or {}
    assert "counts" in summary and summary["total_records"] >= 3
    # The audit summary carries counts, never the exported values.
    assert "PO-100042" not in str(summary)


async def test_authorization_and_tenant_confinement(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    document_id = await seed_document_with_data(client, db)

    # A role without data.export is refused.
    forbidden = client.post(
        f"/orgs/northstar/documents/{document_id}/data-exports", headers=REVIEWER
    )
    assert forbidden.status_code == 403

    # A document that does not exist in the org is a 404 (no leak).
    missing = client.post(f"/orgs/northstar/documents/{uuid.uuid4()}/data-exports", headers=AUDITOR)
    assert missing.status_code == 404


async def test_organization_export_is_durable_and_cancellable(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_document_with_data(client, db)
    response = client.post("/orgs/northstar/data-exports", headers=AUDITOR)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "pending"
    export_id = uuid.UUID(body["id"])
    async with db.session_scope() as session:
        stored = (
            await session.execute(select(DataExportJob).where(DataExportJob.id == export_id))
        ).scalar_one()
        queued = (
            await session.execute(
                select(Job).where(
                    Job.job_type == "data_export.build",
                    Job.payload["data_export_id"].as_string() == str(export_id),
                )
            )
        ).scalar_one()
        assert stored.scope == "organization"
        assert queued.status == "pending"
    cancelled = client.post(
        f"/orgs/northstar/data-exports/{export_id}/cancel",
        json={"reason": "requested in error"},
        headers=AUDITOR,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
