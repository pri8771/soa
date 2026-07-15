import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.outbox import OutboxEvent, enqueue_event
from soa_worker.outbox_publisher import publish_outbox_event


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/publisher.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_publisher_sends_idempotency_key_and_marks_event(db: DatabaseSessions) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    async with db.session_scope() as session:
        event = await enqueue_event(
            session,
            event_type="document.approved",
            payload={"document_id": "doc-1"},
            organization_id=uuid.uuid4(),
        )
        event_id = event.id
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with db.session_scope() as session:
            result = await publish_outbox_event(
                session,
                event_id=event_id,
                destination_url="https://events.example.test/domain-events",
                client=client,
            )
            assert result.outcome == "published"
    assert seen[0].headers["Idempotency-Key"] == str(event_id)
    async with db.session_scope() as session:
        stored = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one()
        assert stored.status == "published"
