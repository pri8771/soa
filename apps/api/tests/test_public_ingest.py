"""Public API ingestion tests (ING-013): key scope, tenant confinement,
idempotent duplicate response, rate limiting, full pipeline reuse."""

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.app import create_app
from soa_api.dependencies import get_db_session
from soa_api.domain.credentials import create_credential
from soa_api.routers import public_ingest as public_ingest_router
from soa_api.services.malware import NoopScanner, ScanResult
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import Document
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext
from soa_storage import MemoryObjectStore, ObjectMetadata

ADMIN = {"X-Dev-User": "user:reviewer"}
PDF = b"%PDF-1.7 api ingested purchase order"


class _TransactionCheckingStore(MemoryObjectStore):
    def __init__(self, current_session: Callable[[], AsyncSession]) -> None:
        super().__init__()
        self._current_session = current_session
        self.transaction_states: list[bool] = []
        self.delete_transaction_states: list[bool] = []

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        sha256: str | None = None,
    ) -> ObjectMetadata:
        self.transaction_states.append(self._current_session().in_transaction())
        return await super().put(
            key,
            data,
            content_type=content_type,
            sha256=sha256,
        )

    async def delete(self, key: str) -> None:
        self.delete_transaction_states.append(self._current_session().in_transaction())
        await super().delete(key)


class _TransactionCheckingScanner(NoopScanner):
    def __init__(self, current_session: Callable[[], AsyncSession]) -> None:
        self._current_session = current_session
        self.transaction_states: list[bool] = []

    async def scan(self, data: bytes) -> ScanResult:
        self.transaction_states.append(self._current_session().in_transaction())
        return await super().scan(data)


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/public-ingest.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            api_ingest_rate_per_minute=3,
            public_ingest_max_file_bytes=1024,
        ),
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
    stream_id = uuid.UUID(
        next(
            item["id"]
            for item in client.get(f"/orgs/{slug}/streams", headers=headers).json()
            if item["slug"] == "email"
        )
    )
    async with db.session_scope() as session:
        credential, raw_key = await create_credential(
            session,
            OrganizationContext(organization_id=org_id),
            name="erp-connector",
            scopes=["documents.upload"],
            allowed_stream_ids=[stream_id],
            actor_id="user:test",
        )
        # A persisted malformed/legacy scope set must still fail closed at
        # authentication. The domain API itself refuses to mint this state.
        if scopes is not None:
            credential.scopes = scopes
    await publish_runtime_config(
        client,
        db,
        organization_slug=slug,
        process_slug="purchase-orders",
        stream_slug="email",
        headers=headers,
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
        job = (
            await session.execute(select(Job).where(Job.job_type == "document.preprocess"))
        ).scalar_one()
        assert job.job_type == "document.preprocess"
        assert job.payload["stream_version_id"]
        assert job.payload["provider_policy_version_id"]
        assert job.payload["confidence_policy_version_id"]
        assert len(job.payload["config_fingerprint"]) == 64
        assert len(job.payload["execution_fingerprint"]) == 64

    # Bad and missing keys fail closed.
    assert ingest(client, "soa_deadbeefdeadbeef_wrong").status_code == 401
    missing = client.post(
        "/v1/streams/email/documents", files={"file": ("po.pdf", PDF, "application/pdf")}
    )
    assert missing.status_code == 401


async def test_scanner_and_object_store_run_outside_the_sql_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/public-boundary.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    active_session: dict[str, AsyncSession] = {}

    def current_session() -> AsyncSession:
        return active_session["session"]

    store = _TransactionCheckingStore(current_session)
    scanner = _TransactionCheckingScanner(current_session)
    app = create_app(
        ApiSettings(environment=Environment.TEST, public_ingest_max_file_bytes=1024),
        db=db,
        object_store=store,
        malware_scanner=scanner,
    )

    async def tracked_session() -> AsyncIterator[AsyncSession]:
        async with db.session_scope() as session:
            active_session["session"] = session
            yield session

    app.dependency_overrides[get_db_session] = tracked_session
    with TestClient(app, raise_server_exceptions=False) as client:
        raw_key = await seed_org_stream_and_key(client, db)
        response = ingest(client, raw_key)

        async def fail_finalization(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced registration failure")

        monkeypatch.setattr(
            public_ingest_router,
            "finalize_document_intake",
            fail_finalization,
        )
        failed = ingest(client, raw_key, data=PDF + b" second")

    assert response.status_code == 201, response.text
    assert failed.status_code == 500
    assert scanner.transaction_states == [False, False]
    assert store.transaction_states == [False, False]
    assert store.delete_transaction_states == [False]


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


async def test_key_cannot_ingest_to_an_unlisted_stream_in_its_own_tenant(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    raw_key = await seed_org_stream_and_key(client, db)
    assert (
        client.post(
            "/orgs/northstar/processes/purchase-orders/streams",
            json={"name": "Restricted", "slug": "restricted"},
            headers=ADMIN,
        ).status_code
        == 201
    )
    # Unauthorized and nonexistent streams are intentionally indistinguishable.
    denied = ingest(client, raw_key, stream="restricted")
    missing = ingest(client, raw_key, stream="does-not-exist")
    assert denied.status_code == missing.status_code == 404
    assert denied.json()["error"]["message"] == missing.json()["error"]["message"]


async def test_legacy_key_without_stream_scope_fails_closed(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_org_stream_and_key(client, db)
    org_id = uuid.UUID(client.get("/orgs/northstar", headers=ADMIN).json()["id"])
    async with db.session_scope() as session:
        _credential, legacy_key = await create_credential(
            session,
            OrganizationContext(organization_id=org_id),
            name="pre-stream-scope key",
            scopes=["documents.upload"],
            actor_id="user:test",
        )
    assert ingest(client, legacy_key).status_code == 404


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


async def test_multipart_upload_is_bounded_while_streaming(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    raw_key = await seed_org_stream_and_key(client, db)

    response = ingest(client, raw_key, data=b"%PDF-1.7 " + b"x" * 1024)
    assert response.status_code == 422
    assert "1024-byte limit" in response.json()["error"]["message"]

    async with db.session_scope() as session:
        assert (await session.execute(select(Document))).scalars().all() == []


async def test_invalid_api_key_lookups_are_pre_auth_rate_limited(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/auth-limit.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST, rate_limit_public_auth_per_minute=1),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        first = ingest(client, "soa_deadbeef_wrong")
        second = ingest(client, "soa_deadbeef_wrong")
        assert first.status_code == 401
        assert second.status_code == 429
        assert second.headers["Retry-After"] == "60"
