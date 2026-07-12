"""Process and process-version lifecycle tests (CFG-001)."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_api.domain.processes import (
    ImmutableVersionError,
    InvalidVersionStateError,
    ProcessRepository,
    ProcessVersionRepository,
    VersionState,
    create_draft,
    create_process,
    publish_draft,
    set_active_version,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.mixins import VersionConflictError
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ORG_B = OrganizationContext(organization_id=uuid.UUID(int=0xB))
ACTOR = "user:test-admin"


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/processes.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_published(db: DatabaseSessions) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a process with one published version; return (process_id, version_id)."""
    async with db.session_scope() as session:
        process = await create_process(
            session, ORG_A, name="Purchase orders", slug="purchase-orders", actor_id=ACTOR
        )
        draft = await create_draft(
            session, ORG_A, process=process, definition={"fields": ["po_number"]}, actor_id=ACTOR
        )
        await publish_draft(session, ORG_A, process=process, draft=draft, actor_id=ACTOR)
        return process.id, draft.id


async def test_draft_is_editable_with_version_check(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        process = await create_process(session, ORG_A, name="POs", slug="pos", actor_id=ACTOR)
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        draft_id = draft.id
    async with db.session_scope() as session:
        draft = await ProcessVersionRepository(session, ORG_A).get(draft_id)
        assert draft is not None
        draft.expect_version(draft.version)  # optimistic precondition holds
        draft.definition = {"fields": ["total"]}
    async with db.session_scope() as session:
        stored = await ProcessVersionRepository(session, ORG_A).get(draft_id)
        assert stored is not None
        assert stored.definition == {"fields": ["total"]}
        with pytest.raises(VersionConflictError):
            stored.expect_version(stored.version + 41)


async def test_publish_sets_active_pointer_and_audits(db: DatabaseSessions) -> None:
    process_id, version_id = await seed_published(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        assert process.active_version_id == version_id
        published = await ProcessVersionRepository(session, ORG_A).get(version_id)
        assert published is not None
        assert published.state == VersionState.PUBLISHED
        assert published.published_by == ACTOR
        actions = (await session.execute(select(AuditEvent.action))).scalars().all()
        assert "process.version_published" in actions


async def test_second_publish_supersedes_previous(db: DatabaseSessions) -> None:
    process_id, first_version_id = await seed_published(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        await publish_draft(session, ORG_A, process=process, draft=draft, actor_id=ACTOR)
        second_id = draft.id
    async with db.session_scope() as session:
        repo = ProcessVersionRepository(session, ORG_A)
        first = await repo.get(first_version_id)
        second = await repo.get(second_id)
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert first is not None and first.state == VersionState.SUPERSEDED
        assert second is not None and second.state == VersionState.PUBLISHED
        assert process is not None and process.active_version_id == second_id


async def test_published_versions_are_immutable(db: DatabaseSessions) -> None:
    _, version_id = await seed_published(db)
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            published = await ProcessVersionRepository(session, ORG_A).get(version_id)
            assert published is not None
            published.definition = {"fields": ["tampered"]}
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            published = await ProcessVersionRepository(session, ORG_A).get(version_id)
            assert published is not None
            await session.delete(published)


async def test_superseded_versions_are_immutable_too(db: DatabaseSessions) -> None:
    process_id, first_version_id = await seed_published(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        await publish_draft(session, ORG_A, process=process, draft=draft, actor_id=ACTOR)
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            superseded = await ProcessVersionRepository(session, ORG_A).get(first_version_id)
            assert superseded is not None
            superseded.change_summary = "rewriting history"


async def test_only_drafts_can_publish(db: DatabaseSessions) -> None:
    process_id, version_id = await seed_published(db)
    with pytest.raises(InvalidVersionStateError):
        async with db.session_scope() as session:
            process = await ProcessRepository(session, ORG_A).get(process_id)
            published = await ProcessVersionRepository(session, ORG_A).get(version_id)
            assert process is not None and published is not None
            await publish_draft(session, ORG_A, process=process, draft=published, actor_id=ACTOR)


async def test_rollback_moves_pointer_to_superseded_version_with_audit(
    db: DatabaseSessions,
) -> None:
    process_id, first_version_id = await seed_published(db)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
        await publish_draft(session, ORG_A, process=process, draft=draft, actor_id=ACTOR)
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        first = await ProcessVersionRepository(session, ORG_A).get(first_version_id)
        assert process is not None and first is not None
        await set_active_version(
            session, ORG_A, process=process, version=first, actor_id=ACTOR, reason="bad mapping"
        )
    async with db.session_scope() as session:
        process = await ProcessRepository(session, ORG_A).get(process_id)
        assert process is not None
        assert process.active_version_id == first_version_id
        event = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "process.active_version_changed")
                )
            )
            .scalars()
            .all()[-1]
        )
        assert event.summary["reason"] == "bad mapping"
        assert event.summary["to_version_id"] == str(first_version_id)


async def test_active_pointer_never_references_a_draft(db: DatabaseSessions) -> None:
    process_id, _ = await seed_published(db)
    with pytest.raises(InvalidVersionStateError):
        async with db.session_scope() as session:
            process = await ProcessRepository(session, ORG_A).get(process_id)
            assert process is not None
            draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
            await set_active_version(session, ORG_A, process=process, version=draft, actor_id=ACTOR)


async def test_processes_are_tenant_scoped(db: DatabaseSessions) -> None:
    process_id, version_id = await seed_published(db)
    async with db.session_scope() as session:
        assert await ProcessRepository(session, ORG_B).get(process_id) is None
        assert await ProcessVersionRepository(session, ORG_B).get(version_id) is None
        assert await ProcessRepository(session, ORG_B).get_by_slug("purchase-orders") is None
