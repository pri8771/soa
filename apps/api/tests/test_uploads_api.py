"""Upload-session API tests (ING-002): happy path, duplicate complete,
wrong size/hash, unauthorized stream, expired session, policy gates."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.domain.uploads import UploadSession
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import Artifact
from soa_db.documents import Document
from soa_storage import MemoryObjectStore, sha256_hex

ADMIN = {"X-Dev-User": "user:reviewer"}
OUTSIDER = {"X-Dev-User": "user:supervisor"}
PDF_BYTES = b"%PDF-1.7 fake purchase order"


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/uploads-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    settings = ApiSettings(
        environment=Environment.TEST,
        max_upload_bytes=1_000_000,
        max_pending_upload_sessions=2,
    )
    app = create_app(settings, db=db, object_store=store)
    return TestClient(app, raise_server_exceptions=False), db, store


def seed_stream(client: TestClient) -> None:
    assert (
        client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/orgs/northstar/processes",
            json={"name": "Purchase orders", "slug": "purchase-orders"},
            headers=ADMIN,
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/orgs/northstar/processes/purchase-orders/streams",
            json={"name": "Email intake", "slug": "email"},
            headers=ADMIN,
        ).status_code
        == 201
    )


def declare(
    client: TestClient,
    *,
    data: bytes = PDF_BYTES,
    content_type: str = "application/pdf",
    client_reference: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, object]:
    response = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "po-4711.pdf",
            "content_type": content_type,
            "size_bytes": size_bytes if size_bytes is not None else len(data),
            "sha256": sha256_hex(data),
            "client_reference": client_reference,
        },
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def upload_bytes(
    store: MemoryObjectStore, session_payload: dict[str, object], data: bytes
) -> None:
    key = store.verify_signed_url(str(session_payload["upload_url"]))
    await store.put(key, data, content_type="application/pdf")


async def test_happy_path_and_idempotent_complete(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)
    payload = declare(client, client_reference="batch-42")
    assert payload["upload_method"] == "PUT"
    await upload_bytes(store, payload, PDF_BYTES)

    completed = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["document_id"] == payload["document_id"]
    assert completed.json()["state"] == "queued"

    # Retrying complete is safe: same document, no second delivery.
    again = client.post(f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN)
    assert again.status_code == 200
    assert again.json()["document_id"] == payload["document_id"]

    async with db.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        artifacts = (await session.execute(select(Artifact))).scalars().all()
        assert len(documents) == 1
        assert len(artifacts) == 1
        assert str(documents[0].id) == payload["document_id"]
        assert documents[0].client_reference == "batch-42"
        assert artifacts[0].object_key.startswith("orgs/")

    # The same client reference cannot open a second delivery.
    duplicate = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "po-4711.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(PDF_BYTES),
            "sha256": sha256_hex(PDF_BYTES),
            "client_reference": "batch-42",
        },
        headers=ADMIN,
    )
    assert duplicate.status_code == 409
    assert payload["document_id"] in duplicate.text


async def test_wrong_bytes_block_completion_until_fixed(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _db, store = harness
    seed_stream(client)
    payload = declare(client)  # declares PDF_BYTES
    await upload_bytes(store, payload, b"entirely different bytes")

    refused = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert refused.status_code == 422
    assert "mismatch" in refused.text

    # Completing with no object at all is a clear conflict.
    other = declare(client)
    missing = client.post(f"/orgs/northstar/uploads/{other['session_id']}/complete", headers=ADMIN)
    assert missing.status_code == 409
    assert "No object" in missing.text

    # Re-uploading the declared bytes lets completion succeed.
    await upload_bytes(store, payload, PDF_BYTES)
    fixed = client.post(f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN)
    assert fixed.status_code == 200, fixed.text


async def test_policy_gates_run_before_signing(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _db, _store = harness
    seed_stream(client)

    unsupported = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "macro.docm",
            "content_type": "application/vnd.ms-word.document.macroEnabled.12",
            "size_bytes": 100,
            "sha256": "a" * 64,
        },
        headers=ADMIN,
    )
    assert unsupported.status_code == 422
    assert "unsupported content type" in unsupported.text

    oversized = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "huge.pdf",
            "content_type": "application/pdf",
            "size_bytes": 2_000_000,  # harness max is 1 MB
            "sha256": "a" * 64,
        },
        headers=ADMIN,
    )
    assert oversized.status_code == 422
    assert "exceeds" in oversized.text

    # Quota: harness allows 2 pending sessions.
    declare(client)
    declare(client)
    quota = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "third.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "sha256": "a" * 64,
        },
        headers=ADMIN,
    )
    assert quota.status_code == 422
    assert "pending upload sessions" in quota.text


async def test_unauthorized_stream_and_cross_tenant_sessions(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _db, _store = harness
    seed_stream(client)
    payload = declare(client)

    # No membership: stream existence must not leak.
    denied = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "x.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "sha256": "a" * 64,
        },
        headers=OUTSIDER,
    )
    assert denied.status_code == 404

    # A different organization cannot complete northstar's session.
    assert (
        client.post(
            "/organizations", json={"name": "Other", "slug": "other-org"}, headers=OUTSIDER
        ).status_code
        == 201
    )
    cross = client.post(
        f"/orgs/other-org/uploads/{payload['session_id']}/complete", headers=OUTSIDER
    )
    assert cross.status_code == 404


async def test_expired_sessions_answer_410_and_stay_expired(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    seed_stream(client)
    payload = declare(client)
    await upload_bytes(store, payload, PDF_BYTES)

    async with db.session_scope() as session:
        record = (
            await session.execute(
                select(UploadSession).where(
                    UploadSession.id == uuid.UUID(str(payload["session_id"]))
                )
            )
        ).scalar_one()
        record.expires_at = record.expires_at - timedelta(days=2)

    gone = client.post(f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN)
    assert gone.status_code == 410
    again = client.post(f"/orgs/northstar/uploads/{payload['session_id']}/abort", headers=ADMIN)
    assert again.status_code == 410

    async with db.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert documents == []


async def test_abort_discards_uploaded_bytes(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _db, store = harness
    seed_stream(client)
    payload = declare(client)
    await upload_bytes(store, payload, PDF_BYTES)

    aborted = client.post(f"/orgs/northstar/uploads/{payload['session_id']}/abort", headers=ADMIN)
    assert aborted.status_code == 200
    assert aborted.json()["state"] == "aborted"
    assert await store.list_keys() == [], "aborting removes the uploaded object"

    # An aborted session cannot be completed.
    refused = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert refused.status_code == 409
