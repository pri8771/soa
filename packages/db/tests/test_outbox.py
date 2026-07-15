import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.jobs import Job
from soa_db.mixins import UuidPrimaryKeyMixin
from soa_db.outbox import (
    OutboxEvent,
    OutboxStatus,
    claim_pending_events,
    enqueue_event,
    mark_failed,
    mark_published,
    replay_failed_event,
)
from soa_db.types import utcnow


class Account(UuidPrimaryKeyMixin, Base):
    __tablename__ = "test_account"
    name: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/outbox.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_domain_state_and_event_commit_atomically(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        session.add(Account(name="acme"))
        await enqueue_event(
            session,
            event_type="account.created",
            payload={"name": "acme"},
            organization_id=uuid.UUID(int=1),
            correlation_id="corr-outbox-1",
        )
    async with sessions.session_scope() as session:
        accounts = (await session.execute(select(Account))).scalars().all()
        events = (await session.execute(select(OutboxEvent))).scalars().all()
    assert len(accounts) == 1
    assert len(events) == 1
    assert events[0].status == OutboxStatus.PENDING
    assert events[0].correlation_id == "corr-outbox-1"
    await sessions.dispose()


async def test_rollback_discards_domain_state_and_event_together(
    sessions: DatabaseSessions,
) -> None:
    with pytest.raises(RuntimeError, match="fail-after-enqueue"):
        async with sessions.session_scope() as session:
            session.add(Account(name="doomed"))
            await enqueue_event(session, event_type="account.created", payload={})
            raise RuntimeError("fail-after-enqueue")
    async with sessions.session_scope() as session:
        accounts = (await session.execute(select(Account))).scalars().all()
        events = (await session.execute(select(OutboxEvent))).scalars().all()
    assert accounts == []
    assert events == [], "an event must never survive a rolled-back domain change"
    await sessions.dispose()


async def test_dedupe_key_absorbs_duplicate_enqueue(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        first = await enqueue_event(
            session, event_type="export.requested", payload={"doc": 1}, dedupe_key="export:doc:1"
        )
        await session.flush()
        second = await enqueue_event(
            session, event_type="export.requested", payload={"doc": 1}, dedupe_key="export:doc:1"
        )
        assert second.id == first.id
    async with sessions.session_scope() as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
    assert len(events) == 1
    await sessions.dispose()


async def test_publisher_flow_and_duplicate_publication_safety(
    sessions: DatabaseSessions,
) -> None:
    async with sessions.session_scope() as session:
        await enqueue_event(session, event_type="a", payload={})
    async with sessions.session_scope() as session:
        claimed = await claim_pending_events(session)
        assert len(claimed) == 1
        event = claimed[0]
        mark_published(event)
        first_published_at = event.published_at
        mark_published(event)  # duplicate publication is a no-op
        assert event.published_at == first_published_at
    async with sessions.session_scope() as session:
        remaining = await claim_pending_events(session)
        assert remaining == [], "published events must not be claimed again"
    await sessions.dispose()


async def test_failed_publication_retries_with_backoff_then_dead_letters(
    sessions: DatabaseSessions,
) -> None:
    async with sessions.session_scope() as session:
        event = await enqueue_event(session, event_type="a", payload={})
        await session.flush()
        before = utcnow()
        mark_failed(event, error="connection refused", retry_in=timedelta(seconds=30))
        assert event.status == OutboxStatus.PENDING
        assert event.attempts == 1
        assert event.last_error == "connection refused"
        assert event.next_attempt_at >= before + timedelta(seconds=29)

        not_due = await claim_pending_events(session)
        assert not_due == [], "future next_attempt_at must defer claiming"

        for _ in range(9):
            mark_failed(event, error="still down", max_attempts=10)
        assert event.status == OutboxStatus.FAILED, "attempts exhausted -> dead letter"
    await sessions.dispose()


async def test_terminal_publication_failure_dead_letters_immediately(
    sessions: DatabaseSessions,
) -> None:
    async with sessions.session_scope() as session:
        event = await enqueue_event(session, event_type="a", payload={})
        mark_failed(event, error="receiver rejected request", terminal=True)
        assert event.status == OutboxStatus.FAILED
        assert event.attempts == 1
    await sessions.dispose()


async def test_replay_resets_delivery_state_and_grants_a_fresh_attempt_budget(
    sessions: DatabaseSessions,
) -> None:
    async with sessions.session_scope() as session:
        event = await enqueue_event(
            session,
            event_type="account.created",
            payload={},
            organization_id=uuid.uuid4(),
        )
        mark_failed(event, error="delivery failed", max_attempts=1)
        event.published_at = utcnow()  # stale/corrupt delivery state must not survive replay
        event.next_attempt_at = utcnow() + timedelta(days=1)

        previous_attempts = await replay_failed_event(session, event)
        assert previous_attempts == 1
        assert event.status == OutboxStatus.PENDING
        assert event.attempts == 0
        assert event.published_at is None
        assert event.last_error is None
        assert event.next_attempt_at <= utcnow()

        # Exhaust and replay again to prove replay generations do not collide
        # merely because the reset attempt count reaches the same value.
        mark_failed(event, error="delivery failed again", max_attempts=1)
        assert await replay_failed_event(session, event) == 1
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 3  # initial publication plus two distinct replays

    await sessions.dispose()
