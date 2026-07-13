"""Public API ingestion tests (ING-013): key scope, tenant confinement,
idempotent duplicate response, rate limiting, full pipeline reuse."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.credentials import create_credential
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import Document
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:reviewer"}
PDF = b"%PDF-1.7 api ingested purchase order"


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/public-ingest.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST, api_ingest_rate_per_minute=3),
        db=db,
        object_store=MemoryObjectStore(),
    )
    # The limiter lives on the app's Dependencies now (SEC-003), so
    # each create_app is isolated by construction.
    return TestClient(app, raise_server_exceptions=False), db


async def seed_org_stream_and_key(
    client: TestClient,
    db: DatabaseSessions,
    *,
    slug: str = "northstar",
    scopes: list[str] | None = None,
    headers: dict[str, str] = ADMIN,
) -> str:
    for path, body in (
        ("/organizations", {"name": slug.title(), "slug": slug}),
        (f"/orgs/{slug}/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            f"/orgs/{slug}/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=headers).status_code == 201
    org_id = uuid.UUID(client.get(f"/orgs/{slug}", headers=headers).json()["id"])
    async with db.session_scope() as session:
        _credential, raw_key = await create_credential(
            session,
            OrganizationContext(organization_id=org_id),
            name="erp-connector",
            scopes=scopes if scopes is not None else ["documents.upload"],
            actor_id="user:test",
        )
    return raw_key


def ingest(
    client: TestClient,
    raw_key: str,
    *,
    stream: str = "email",
    data: bytes = PDF,
    client_reference: str | None = None,
):
    form: dict[str, str] = {}
    if client_reference is not None:
        form["client_reference"] = client_reference
    return client.post(
        f"/v1/streams/{stream}/documents",
        files={"file": ("po.pdf", data, "application/pdf")},
        data=form,
        headers={"X-Api-Key": raw_key},
    )


async def test_key_scoped_ingestion_runs_the_full_pipeline(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    raw_key = await seed_org_stream_and_key(client, db)

    response = ingest(client, raw_key, client_reference="erp-42")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "queued"

    async with db.session_scope() as session:
        document = (await session.execute(select(Document))).scalars().one()
        assert document.source_channel == "api"
        assert document.client_reference == "erp-42"
        job = (await session.execute(select(Job))).scalars().one()
        assert job.job_type == "document.preprocess"

    # Bad and missing keys fail closed.
    assert ingest(client, "soa_deadbeefdeadbeef_wrong").status_code == 401
    missing = client.post(
        "/v1/streams/email/documents", files={"file": ("po.pdf", PDF, "application/pdf")}
    )
    assert missing.status_code == 401


async def test_scope_and_tenant_confinement(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    # Key without the upload scope: 403.
    read_only = await seed_org_stream_and_key(client, db, scopes=["documents.read"])
    assert ingest(client, read_only).status_code == 403

    # A second org's key cannot reach northstar's stream by name: its own
    # org simply has no such stream -> 404, nothing leaks.
    other_key = await seed_org_stream_and_key(
        client, db, slug="other-org", headers={"X-Dev-User": "user:supervisor"}
    )
    # other-org DOES have a stream called "email", so use a northstar-only
    # slug to prove lookups happen in the key's org.
    assert (
        client.post(
            "/orgs/northstar/processes/purchase-orders/streams",
            json={"name": "Portal", "slug": "portal-only"},
            headers=ADMIN,
        ).status_code
        == 201
    )
    confined = ingest(client, other_key, stream="portal-only")
    assert confined.status_code == 404


async def test_repeated_client_reference_is_a_safe_replay(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    raw_key = await seed_org_stream_and_key(client, db)
    first = ingest(client, raw_key, client_reference="po-999")
    assert first.status_code == 201
    replay = ingest(client, raw_key, client_reference="po-999")
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["document_id"] == first.json()["document_id"]

    async with db.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert len(documents) == 1, "a replay never creates a second delivery"


async def test_rate_limit_answers_429_with_retry_after(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    raw_key = await seed_org_stream_and_key(client, db)
    # Harness limit is 3/minute; distinct payloads avoid duplicate handling.
    for index in range(3):
        assert ingest(client, raw_key, data=PDF + str(index).encode()).status_code == 201
    limited = ingest(client, raw_key, data=PDF + b"overflow")
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 61
    assert limited.headers["X-RateLimit-Remaining"] == "0"
