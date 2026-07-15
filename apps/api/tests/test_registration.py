"""Atomic registration tests (ING-007): exactly-once job/outbox on
success, full rollback on failure with an explainable session state."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import soa_api.services.ingestion as ingestion_module
from soa_api.app import create_app
from soa_api.domain.uploads import UploadSession
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import Artifact
from soa_db.documents import Document
from soa_db.jobs import Job
from soa_db.outbox import OutboxEvent
from soa_storage import MemoryObjectStore, sha256_hex

ADMIN = {"X-Dev-User": "user:reviewer"}
PDF = b"%PDF-1.7 registration test"


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/registration.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(ApiSettings(environment=Environment.TEST), db=db, object_store=store)
    return TestClient(app, raise_server_exceptions=False), db, store


async def seed_stream(client: TestClient, db: DatabaseSessions) -> None:
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    await publish_runtime_config(client, db)


async def open_and_upload(client: TestClient, store: MemoryObjectStore) -> dict[str, str]:
    created = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": "po.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(PDF),
            "sha256": sha256_hex(PDF),
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    await store.put(store.verify_signed_url(payload["upload_url"]), PDF)
    return dict(payload)


async def count_rows(db: DatabaseSessions) -> dict[str, int]:
    async with db.session_scope() as session:
        return {
            "documents": len((await session.execute(select(Document))).scalars().all()),
            "artifacts": len((await session.execute(select(Artifact))).scalars().all()),
            "jobs": len((await session.execute(select(Job))).scalars().all()),
            "outbox": len((await session.execute(select(OutboxEvent))).scalars().all()),
        }


async def test_completion_registers_everything_exactly_once(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    await seed_stream(client, db)
    payload = await open_and_upload(client, store)

    completed = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "queued"

    # Concurrent/duplicate complete: same document, nothing enqueued twice.
    for _ in range(2):
        again = client.post(
            f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
        )
        assert again.status_code == 200

    counts = await count_rows(db)
    assert counts == {"documents": 1, "artifacts": 1, "jobs": 2, "outbox": 1}

    async with db.session_scope() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "document.preprocess"))
        ).scalar_one()
        assert job.job_type == "document.preprocess"
        assert job.payload["document_id"] == payload["document_id"]
        assert job.dedupe_key == f"document.preprocess:{payload['document_id']}"
        event = (await session.execute(select(OutboxEvent))).scalars().one()
        assert event.event_type == "document.registered"
        assert event.payload["document_id"] == payload["document_id"]


async def test_failure_rolls_back_registration_and_leaves_session_explainable(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, store = harness
    await seed_stream(client, db)
    payload = await open_and_upload(client, store)

    # Blow up at the very end of registration: everything before it must
    # roll back with it.
    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("outbox wiring failure (simulated)")

    monkeypatch.setattr(ingestion_module, "enqueue_event", explode)
    failed = client.post(f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN)
    assert failed.status_code == 500

    # Nothing partial: no document, artifact, job, or outbox row.
    assert await count_rows(db) == {"documents": 0, "artifacts": 0, "jobs": 0, "outbox": 0}
    # The session state explains where things stand: still pending.
    async with db.session_scope() as session:
        record = (
            await session.execute(
                select(UploadSession).where(
                    UploadSession.id == uuid.UUID(str(payload["session_id"]))
                )
            )
        ).scalar_one()
        assert record.state == "pending"

    # The wiring recovers: a retry registers everything exactly once.
    monkeypatch.undo()
    retried = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert retried.status_code == 200, retried.text
    assert await count_rows(db) == {"documents": 1, "artifacts": 1, "jobs": 2, "outbox": 1}
