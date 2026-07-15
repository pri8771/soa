import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.outbox import OutboxEvent, OutboxStatus, enqueue_event, mark_failed
from soa_db.types import utcnow

ADMIN = {"X-Dev-User": "user:admin"}


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, uuid.UUID]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/outbox-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    client = TestClient(
        create_app(ApiSettings(environment=Environment.TEST), db=db),
        raise_server_exceptions=False,
    )
    response = client.post(
        "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
    )
    assert response.status_code == 201
    return client, db, uuid.UUID(response.json()["id"])


async def test_replay_resets_delivery_state_and_preserves_previous_attempts_in_audit(
    harness: tuple[TestClient, DatabaseSessions, uuid.UUID],
) -> None:
    client, db, organization_id = harness
    async with db.session_scope() as session:
        event = await enqueue_event(
            session,
            event_type="document.approved",
            payload={"document_id": str(uuid.uuid4())},
            organization_id=organization_id,
        )
        mark_failed(event, error="destination unavailable", max_attempts=1)
        event.published_at = utcnow()
        event.next_attempt_at = utcnow() + timedelta(days=1)
        event_id = event.id

    response = client.post(f"/orgs/northstar/outbox/{event_id}/replay", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"id": str(event_id), "status": "pending", "attempts": 0}

    async with db.session_scope() as session:
        stored = await session.get(OutboxEvent, event_id)
        assert stored is not None
        assert stored.status == OutboxStatus.PENDING
        assert stored.attempts == 0
        assert stored.published_at is None
        assert stored.last_error is None
        assert stored.next_attempt_at <= utcnow()

        audit = (
            await session.execute(select(AuditEvent).where(AuditEvent.action == "outbox.replayed"))
        ).scalar_one()
        assert audit.target_id == str(event_id)
        assert audit.summary["previous_attempts"] == 1

    await db.dispose()
