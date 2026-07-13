"""Artifact model tests (STO-003): append-only identity, tenant scope,
key uniqueness, audit trail."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.artifacts import (
    Artifact,
    ArtifactImmutableError,
    ArtifactKind,
    ArtifactRepository,
    RetentionClass,
    create_artifact,
)
from soa_db.audit import AuditEvent
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
SHA = "a" * 64


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/artifacts.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_artifact(db: DatabaseSessions, *, key: str = "orgs/a/doc/original/x") -> uuid.UUID:
    async with db.session_scope() as session:
        artifact = await create_artifact(
            session,
            OrganizationContext(organization_id=ORG_A),
            document_id=DOC,
            kind=ArtifactKind.ORIGINAL,
            object_key=key,
            sha256=SHA,
            size_bytes=1234,
            content_type="application/pdf",
            produced_by_stage="intake",
        )
        return artifact.id


async def test_create_records_metadata_and_audit_event(db: DatabaseSessions) -> None:
    artifact_id = await make_artifact(db)
    async with db.session_scope() as session:
        stored = await ArtifactRepository(session, OrganizationContext(organization_id=ORG_A)).get(
            artifact_id
        )
        assert stored is not None
        assert (stored.kind, stored.sha256, stored.size_bytes) == ("original", SHA, 1234)
        assert stored.retention_class == RetentionClass.STANDARD.value
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "artifact.created")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].summary["sha256"] == SHA


async def test_identity_fields_are_immutable(db: DatabaseSessions) -> None:
    artifact_id = await make_artifact(db)
    context = OrganizationContext(organization_id=ORG_A)
    for field, value in (
        ("sha256", "b" * 64),
        ("object_key", "orgs/a/doc/original/replaced"),
        ("size_bytes", 1),
        ("kind", ArtifactKind.EXTRACTION.value),
        ("document_id", uuid.uuid4()),
    ):
        with pytest.raises(ArtifactImmutableError, match=field):
            async with db.session_scope() as session:
                stored = await ArtifactRepository(session, context).get(artifact_id)
                assert stored is not None
                setattr(stored, field, value)
                await session.flush()

    # Nothing leaked through.
    async with db.session_scope() as session:
        stored = await ArtifactRepository(session, context).get(artifact_id)
        assert stored is not None
        assert stored.sha256 == SHA and stored.size_bytes == 1234


async def test_retention_class_is_the_one_mutable_field(db: DatabaseSessions) -> None:
    artifact_id = await make_artifact(db)
    context = OrganizationContext(organization_id=ORG_A)
    async with db.session_scope() as session:
        stored = await ArtifactRepository(session, context).get(artifact_id)
        assert stored is not None
        stored.retention_class = RetentionClass.LEGAL_HOLD.value
        await session.flush()
    async with db.session_scope() as session:
        stored = await ArtifactRepository(session, context).get(artifact_id)
        assert stored is not None
        assert stored.retention_class == "legal_hold"


async def test_object_keys_cannot_be_claimed_twice(db: DatabaseSessions) -> None:
    await make_artifact(db, key="orgs/a/doc/original/unique")
    with pytest.raises(IntegrityError):
        await make_artifact(db, key="orgs/a/doc/original/unique")


async def test_repository_scope_hides_other_tenants(db: DatabaseSessions) -> None:
    artifact_id = await make_artifact(db)
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await ArtifactRepository(session, other).get(artifact_id) is None
        assert await ArtifactRepository(session, other).list_for_document(DOC) == []
        mine = OrganizationContext(organization_id=ORG_A)
        listed = await ArtifactRepository(session, mine).list_for_document(DOC)
        assert [a.id for a in listed] == [artifact_id]


async def test_create_rejects_malformed_hashes_and_sizes(db: DatabaseSessions) -> None:
    context = OrganizationContext(organization_id=ORG_A)
    async with db.session_scope() as session:
        with pytest.raises(ValueError, match="sha256"):
            await create_artifact(
                session,
                context,
                document_id=DOC,
                kind=ArtifactKind.ORIGINAL,
                object_key="k1",
                sha256="not-hex",
                size_bytes=1,
                content_type="application/pdf",
            )
        with pytest.raises(ValueError, match="size_bytes"):
            await create_artifact(
                session,
                context,
                document_id=DOC,
                kind=ArtifactKind.ORIGINAL,
                object_key="k2",
                sha256=SHA,
                size_bytes=-1,
                content_type="application/pdf",
            )


async def test_direct_model_reference() -> None:
    # The table participates in RLS defense in depth (tenant_guard) — a
    # regression here means the migration and the guard list diverged.
    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert Artifact.__tablename__ in RLS_PROTECTED_TABLES
