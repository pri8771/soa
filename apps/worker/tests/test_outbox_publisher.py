import hashlib
import hmac
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
    organization_id = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    async with db.session_scope() as session:
        event = await enqueue_event(
            session,
            event_type="document.approved",
            payload={"document_id": "doc-1"},
            organization_id=organization_id,
        )
        event_id = event.id
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await publish_outbox_event(
            db,
            event_id=event_id,
            expected_organization_id=organization_id,
            destination_url="https://events.example.test/domain-events",
            client=client,
            signing_secret="s" * 32,
        )
        assert result.outcome == "published"
    assert seen[0].headers["Idempotency-Key"] == str(event_id)
    timestamp = seen[0].headers["X-SOA-Timestamp"]
    expected = hmac.new(
        ("s" * 32).encode(),
        timestamp.encode() + b"." + seen[0].content,
        hashlib.sha256,
    ).hexdigest()
    assert seen[0].headers["X-SOA-Signature"] == f"v1={expected}"
    async with db.session_scope() as session:
        stored = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one()
        assert stored.status == "published"


@pytest.mark.parametrize(
    ("status_code", "outcome", "stored_status"),
    [
        (400, "dead_letter", "failed"),
        (401, "dead_letter", "failed"),
        (429, "retryable_error", "pending"),
        (503, "retryable_error", "pending"),
    ],
)
async def test_publisher_classifies_permanent_and_retryable_http_failures(
    db: DatabaseSessions,
    status_code: int,
    outcome: str,
    stored_status: str,
) -> None:
    async with db.session_scope() as session:
        event = await enqueue_event(session, event_type="document.approved", payload={})
        event_id = event.id

    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await publish_outbox_event(
            db,
            event_id=event_id,
            expected_organization_id=None,
            destination_url="https://events.example.test/domain-events",
            client=client,
            signing_secret="s" * 32,
        )
        assert result.outcome == outcome
    async with db.session_scope() as session:
        stored = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one()
        assert stored.status == stored_status


async def test_publisher_refuses_queue_tenant_mismatch_before_network(
    db: DatabaseSessions,
) -> None:
    event_organization_id = uuid.uuid4()
    async with db.session_scope() as session:
        event = await enqueue_event(
            session,
            event_type="document.approved",
            payload={},
            organization_id=event_organization_id,
        )
        event_id = event.id

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="queue envelope"):
            await publish_outbox_event(
                db,
                event_id=event_id,
                expected_organization_id=uuid.uuid4(),
                destination_url="https://events.example.test/domain-events",
                client=client,
            )
    assert calls == 0
