import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_config.logging import REDACTED, correlation_context
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import (
    ActorType,
    AuditEvent,
    AuditImmutabilityError,
    record_audit_event,
)

ORG = uuid.UUID(int=0xAB)


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/audit.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def create_event(sessions: DatabaseSessions) -> uuid.UUID:
    async with sessions.session_scope() as session:
        audit = await record_audit_event(
            session,
            actor_type=ActorType.USER,
            actor_id="user:reviewer",
            action="document.approved",
            target_type="document",
            target_id="doc-123",
            organization_id=ORG,
            summary={"po_number": "PO-90001"},
        )
    return audit.id


async def test_audit_event_records_required_fields(sessions: DatabaseSessions) -> None:
    with correlation_context("audit-corr-1"):
        await create_event(sessions)
    async with sessions.session_scope() as session:
        stored = (await session.execute(select(AuditEvent))).scalar_one()
    assert stored.actor_type == "user"
    assert stored.actor_id == "user:reviewer"
    assert stored.action == "document.approved"
    assert stored.target_type == "document"
    assert stored.target_id == "doc-123"
    assert stored.organization_id == ORG
    assert stored.occurred_at.tzinfo is not None
    assert stored.correlation_id == "audit-corr-1", "correlation must flow from ambient context"
    assert stored.summary == {"po_number": "PO-90001"}
    await sessions.dispose()


async def test_summary_is_redacted_before_storage(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        await record_audit_event(
            session,
            actor_type=ActorType.SERVICE,
            actor_id="svc:export",
            action="integration.credential_rotated",
            target_type="integration",
            target_id="int-1",
            organization_id=ORG,
            summary={"api_key": "sk-super-secret", "destination": "erp-webhook"},
        )
    async with sessions.session_scope() as session:
        stored = (await session.execute(select(AuditEvent))).scalar_one()
    assert stored.summary is not None
    assert stored.summary["api_key"] == REDACTED
    assert stored.summary["destination"] == "erp-webhook"
    await sessions.dispose()


async def test_updating_an_audit_event_is_rejected(sessions: DatabaseSessions) -> None:
    event_id = await create_event(sessions)
    with pytest.raises(AuditImmutabilityError, match="cannot be updated"):
        async with sessions.session_scope() as session:
            stored = (
                await session.execute(select(AuditEvent).where(AuditEvent.id == event_id))
            ).scalar_one()
            stored.action = "document.rewritten-history"
    async with sessions.session_scope() as session:
        unchanged = (await session.execute(select(AuditEvent))).scalar_one()
    assert unchanged.action == "document.approved"
    await sessions.dispose()


async def test_deleting_an_audit_event_is_rejected(sessions: DatabaseSessions) -> None:
    event_id = await create_event(sessions)
    with pytest.raises(AuditImmutabilityError, match="cannot be deleted"):
        async with sessions.session_scope() as session:
            stored = (
                await session.execute(select(AuditEvent).where(AuditEvent.id == event_id))
            ).scalar_one()
            await session.delete(stored)
    async with sessions.session_scope() as session:
        survivors = (await session.execute(select(AuditEvent))).scalars().all()
    assert len(survivors) == 1, "audit history must survive deletion attempts"
    await sessions.dispose()
