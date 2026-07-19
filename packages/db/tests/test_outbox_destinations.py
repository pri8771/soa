"""Per-tenant outbox destination tests (EXP-012): CRUD, optimistic
concurrency, RLS-registry membership."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.outbox_destinations import (
    OutboxDestination,
    VersionConflictError,
    delete_destination,
    get_destination,
    upsert_destination,
)

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/outbox-destinations.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_get_destination_is_none_when_unconfigured(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        assert await get_destination(session, ORG_A) is None


async def test_upsert_creates_then_updates_the_single_row(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        created = await upsert_destination(
            session,
            ORG_A,
            destination_url="https://receiver.example.com/hook",
            actor_id="user:admin",
        )
        assert created.destination_url == "https://receiver.example.com/hook"
        assert created.is_active is True
        assert created.version == 1

    async with db.session_scope() as session:
        updated = await upsert_destination(
            session,
            ORG_A,
            destination_url="https://receiver2.example.com/hook",
            is_active=False,
            if_match=1,
            actor_id="user:admin",
        )
        assert updated.id == created.id
        assert updated.destination_url == "https://receiver2.example.com/hook"
        assert updated.is_active is False

    async with db.session_scope() as session:
        fetched = await get_destination(session, ORG_A)
        assert fetched is not None
        assert fetched.destination_url == "https://receiver2.example.com/hook"


async def test_upsert_rejects_a_stale_version(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await upsert_destination(
            session, ORG_A, destination_url="https://receiver.example.com/hook", actor_id="u"
        )

    async with db.session_scope() as session:
        with pytest.raises(VersionConflictError):
            await upsert_destination(
                session,
                ORG_A,
                destination_url="https://other.example.com/hook",
                if_match=99,
                actor_id="u",
            )


async def test_upsert_ignores_if_match_when_no_row_exists_yet(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        # if_match is meaningless for a first-time create — nothing to
        # conflict with.
        created = await upsert_destination(
            session,
            ORG_A,
            destination_url="https://receiver.example.com/hook",
            if_match=5,
            actor_id="u",
        )
        assert created.version == 1


async def test_delete_removes_the_row_and_returns_true(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await upsert_destination(
            session, ORG_A, destination_url="https://receiver.example.com/hook", actor_id="u"
        )

    async with db.session_scope() as session:
        assert await delete_destination(session, ORG_A, actor_id="u") is True

    async with db.session_scope() as session:
        assert await get_destination(session, ORG_A) is None


async def test_delete_is_false_when_nothing_to_delete(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        assert await delete_destination(session, ORG_A, actor_id="u") is False


async def test_delete_rejects_a_stale_version(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await upsert_destination(
            session, ORG_A, destination_url="https://receiver.example.com/hook", actor_id="u"
        )

    async with db.session_scope() as session:
        with pytest.raises(VersionConflictError):
            await delete_destination(session, ORG_A, if_match=99, actor_id="u")


async def test_destinations_are_scoped_per_organization(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await upsert_destination(
            session, ORG_A, destination_url="https://a.example.com/hook", actor_id="u"
        )
        await upsert_destination(
            session, ORG_B, destination_url="https://b.example.com/hook", actor_id="u"
        )

    async with db.session_scope() as session:
        a = await get_destination(session, ORG_A)
        b = await get_destination(session, ORG_B)
        assert a is not None and a.destination_url == "https://a.example.com/hook"
        assert b is not None and b.destination_url == "https://b.example.com/hook"


async def test_upsert_and_delete_write_audit_events(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await upsert_destination(
            session, ORG_A, destination_url="https://a.example.com/hook", actor_id="u"
        )
    async with db.session_scope() as session:
        await delete_destination(session, ORG_A, actor_id="u")

    async with db.session_scope() as session:
        stmt = select(AuditEvent.action).where(AuditEvent.organization_id == ORG_A)
        actions = (await session.execute(stmt)).scalars().all()
        assert set(actions) == {"outbox_destination.created", "outbox_destination.deleted"}


def test_outbox_destinations_participate_in_rls() -> None:
    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert OutboxDestination.__tablename__ in RLS_PROTECTED_TABLES
