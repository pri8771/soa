"""Stream model tests (CFG-002): inheritance, snapshot self-containment,
immutability, tenant scoping."""

import uuid
from pathlib import Path

import pytest

from soa_api.domain.processes import (
    Process,
    ProcessRepository,
    ProcessVersion,
    create_draft,
    create_process,
    publish_draft,
)
from soa_api.domain.streams import (
    StreamOverrideError,
    StreamRepository,
    StreamVersionRepository,
    create_stream,
    create_stream_draft,
    publish_stream_draft,
)
from soa_api.domain.versioning import (
    ImmutableVersionError,
    InvalidVersionStateError,
    VersionState,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ORG_B = OrganizationContext(organization_id=uuid.UUID(int=0xB))
ACTOR = "user:test-admin"

PROCESS_DEFAULTS = {"language": "en", "confidence_floor": 0.8, "provider": "default-ocr"}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/streams.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_process(db: DatabaseSessions) -> tuple[uuid.UUID, uuid.UUID]:
    """Process with a published version; returns (process_id, process_version_id)."""
    async with db.session_scope() as session:
        process = await create_process(
            session, ORG_A, name="Purchase orders", slug="purchase-orders", actor_id=ACTOR
        )
        draft = await create_draft(
            session, ORG_A, process=process, definition=dict(PROCESS_DEFAULTS), actor_id=ACTOR
        )
        await publish_draft(session, ORG_A, process=process, draft=draft, actor_id=ACTOR)
        return process.id, draft.id


async def load(
    db: DatabaseSessions, session, process_id, version_id
) -> tuple[Process, ProcessVersion]:  # type: ignore[no-untyped-def]
    process = await ProcessRepository(session, ORG_A).get(process_id)
    from soa_api.domain.processes import ProcessVersionRepository

    version = await ProcessVersionRepository(session, ORG_A).get(version_id)
    assert process is not None and version is not None
    return process, version


async def test_overrides_win_and_inherited_values_flow_through(db: DatabaseSessions) -> None:
    process_id, process_version_id = await seed_process(db)
    async with db.session_scope() as session:
        _, process_version = await load(db, session, process_id, process_version_id)
        stream = await create_stream(
            session, ORG_A, process_id=process_id, name="Email intake", slug="email", actor_id=ACTOR
        )
        draft = await create_stream_draft(
            session, ORG_A, stream=stream, overrides={"confidence_floor": 0.95}, actor_id=ACTOR
        )
        published = await publish_stream_draft(
            session,
            ORG_A,
            stream=stream,
            draft=draft,
            process_version=process_version,
            actor_id=ACTOR,
        )
        snapshot = published.resolved_snapshot
        assert snapshot is not None
        assert snapshot["config"]["confidence_floor"] == 0.95, "override wins"
        assert snapshot["config"]["language"] == "en", "inherited from process"
        assert snapshot["config"]["provider"] == "default-ocr"
        assert snapshot["process_version_id"] == str(process_version_id)
        assert len(snapshot["fingerprint"]) == 64
        # Only explicit overrides are stored on the version itself.
        assert published.overrides == {"confidence_floor": 0.95}


@pytest.mark.parametrize("field", ["duplicate_policy", "business_duplicate_policy"])
async def test_publish_revalidates_legacy_invalid_stream_overrides(
    db: DatabaseSessions,
    field: str,
) -> None:
    process_id, process_version_id = await seed_process(db)
    async with db.session_scope() as session:
        _, process_version = await load(db, session, process_id, process_version_id)
        stream = await create_stream(
            session, ORG_A, process_id=process_id, name="Email", slug="email", actor_id=ACTOR
        )
        draft = await create_stream_draft(session, ORG_A, stream=stream, actor_id=ACTOR)
        # Simulate a draft persisted before strict write validation existed.
        draft.overrides = {field: "silent-fallback"}
        with pytest.raises(StreamOverrideError, match=field):
            await publish_stream_draft(
                session,
                ORG_A,
                stream=stream,
                draft=draft,
                process_version=process_version,
                actor_id=ACTOR,
            )
        assert draft.state == VersionState.DRAFT


async def test_snapshot_is_isolated_from_later_process_edits(db: DatabaseSessions) -> None:
    """Acceptance: processing never reads mutable drafts — the snapshot is
    complete and pinned even after the process moves on."""
    process_id, process_version_id = await seed_process(db)
    async with db.session_scope() as session:
        process, process_version = await load(db, session, process_id, process_version_id)
        stream = await create_stream(
            session, ORG_A, process_id=process_id, name="Email intake", slug="email", actor_id=ACTOR
        )
        draft = await create_stream_draft(session, ORG_A, stream=stream, actor_id=ACTOR)
        stream_version = await publish_stream_draft(
            session,
            ORG_A,
            stream=stream,
            draft=draft,
            process_version=process_version,
            actor_id=ACTOR,
        )
        stream_version_id = stream_version.id
    # The process publishes a NEW version with different defaults.
    async with db.session_scope() as session:
        process, _ = await load(db, session, process_id, process_version_id)
        new_draft = await create_draft(
            session, ORG_A, process=process, definition={"language": "de"}, actor_id=ACTOR
        )
        await publish_draft(session, ORG_A, process=process, draft=new_draft, actor_id=ACTOR)
    async with db.session_scope() as session:
        stored = await StreamVersionRepository(session, ORG_A).get(stream_version_id)
        assert stored is not None
        assert stored.resolved_snapshot is not None
        assert stored.resolved_snapshot["config"]["language"] == "en", (
            "published snapshot must not change when the process publishes new versions"
        )
        assert stored.pinned_process_version_id == process_version_id


async def test_published_stream_versions_are_immutable(db: DatabaseSessions) -> None:
    process_id, process_version_id = await seed_process(db)
    async with db.session_scope() as session:
        _, process_version = await load(db, session, process_id, process_version_id)
        stream = await create_stream(
            session, ORG_A, process_id=process_id, name="Email", slug="email", actor_id=ACTOR
        )
        draft = await create_stream_draft(session, ORG_A, stream=stream, actor_id=ACTOR)
        published = await publish_stream_draft(
            session,
            ORG_A,
            stream=stream,
            draft=draft,
            process_version=process_version,
            actor_id=ACTOR,
        )
        published_id = published.id
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            stored = await StreamVersionRepository(session, ORG_A).get(published_id)
            assert stored is not None
            stored.overrides = {"confidence_floor": 0.1}


async def test_stream_cannot_pin_a_process_draft(db: DatabaseSessions) -> None:
    process_id, _ = await seed_process(db)
    with pytest.raises(InvalidVersionStateError):
        async with db.session_scope() as session:
            process = await ProcessRepository(session, ORG_A).get(process_id)
            assert process is not None
            process_draft = await create_draft(session, ORG_A, process=process, actor_id=ACTOR)
            stream = await create_stream(
                session, ORG_A, process_id=process_id, name="Email", slug="email", actor_id=ACTOR
            )
            draft = await create_stream_draft(session, ORG_A, stream=stream, actor_id=ACTOR)
            await publish_stream_draft(
                session,
                ORG_A,
                stream=stream,
                draft=draft,
                process_version=process_draft,
                actor_id=ACTOR,
            )


async def test_second_publish_supersedes_and_moves_pointer(db: DatabaseSessions) -> None:
    process_id, process_version_id = await seed_process(db)
    async with db.session_scope() as session:
        _, process_version = await load(db, session, process_id, process_version_id)
        stream = await create_stream(
            session, ORG_A, process_id=process_id, name="Email", slug="email", actor_id=ACTOR
        )
        first = await create_stream_draft(session, ORG_A, stream=stream, actor_id=ACTOR)
        await publish_stream_draft(
            session,
            ORG_A,
            stream=stream,
            draft=first,
            process_version=process_version,
            actor_id=ACTOR,
        )
        second = await create_stream_draft(
            session, ORG_A, stream=stream, overrides={"provider": "premium-ocr"}, actor_id=ACTOR
        )
        await publish_stream_draft(
            session,
            ORG_A,
            stream=stream,
            draft=second,
            process_version=process_version,
            actor_id=ACTOR,
        )
        first_id, second_id, stream_id = first.id, second.id, stream.id
    async with db.session_scope() as session:
        repo = StreamVersionRepository(session, ORG_A)
        first_stored = await repo.get(first_id)
        second_stored = await repo.get(second_id)
        stream_stored = await StreamRepository(session, ORG_A).get(stream_id)
        assert first_stored is not None and first_stored.state == VersionState.SUPERSEDED
        assert second_stored is not None and second_stored.state == VersionState.PUBLISHED
        assert stream_stored is not None and stream_stored.active_version_id == second_id


async def test_streams_are_tenant_scoped(db: DatabaseSessions) -> None:
    process_id, _ = await seed_process(db)
    async with db.session_scope() as session:
        stream = await create_stream(
            session, ORG_A, process_id=process_id, name="Email", slug="email", actor_id=ACTOR
        )
        stream_id = stream.id
    async with db.session_scope() as session:
        assert await StreamRepository(session, ORG_B).get(stream_id) is None
        assert await StreamRepository(session, ORG_B).get_by_slug("email") is None
