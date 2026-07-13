"""Canonical payload persistence tests (CAN-003): immutable by
(run, schema version), idempotent for identical content, tenant scoped."""

import uuid
from pathlib import Path

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.canonical_payloads import (
    CanonicalPayloadImmutableError,
    CanonicalPayloadRepository,
    payload_sha256,
    record_canonical_payload,
)
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN = uuid.UUID("55555555-5555-4555-8555-555555555555")
CONTEXT = OrganizationContext(organization_id=ORG_A)

PAYLOAD = {"schema_version": "1.0.0", "identifiers": {"po_number": "PO-1"}}


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/canonical.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_identical_content_is_idempotent_different_content_is_refused(
    db: DatabaseSessions,
) -> None:
    async with db.session_scope() as session:
        first = await record_canonical_payload(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            task_id=None,
            schema_version="1.0.0",
            payload=dict(PAYLOAD),
            actor_id="user:x",
        )
        again = await record_canonical_payload(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            task_id=None,
            schema_version="1.0.0",
            payload=dict(PAYLOAD),
            actor_id="user:y",
        )
        assert again.id == first.id  # idempotent, no second row
        assert first.sha256 == payload_sha256(PAYLOAD)
        with pytest.raises(CanonicalPayloadImmutableError):
            await record_canonical_payload(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN,
                task_id=None,
                schema_version="1.0.0",
                payload={"schema_version": "1.0.0", "identifiers": {"po_number": "PO-2"}},
                actor_id="user:x",
            )


async def test_rows_refuse_mutation_and_deletion(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        row = await record_canonical_payload(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            task_id=None,
            schema_version="1.0.0",
            payload=dict(PAYLOAD),
            actor_id="user:x",
        )
        row_id = row.id
    with pytest.raises(CanonicalPayloadImmutableError):
        async with db.session_scope() as session:
            stored = await CanonicalPayloadRepository(session, CONTEXT).get(row_id)
            assert stored is not None
            stored.payload = {"schema_version": "1.0.0"}
            await session.flush()
    with pytest.raises(CanonicalPayloadImmutableError):
        async with db.session_scope() as session:
            stored = await CanonicalPayloadRepository(session, CONTEXT).get(row_id)
            assert stored is not None
            await session.delete(stored)
            await session.flush()
    # The row survives both attempts untouched.
    async with db.session_scope() as session:
        survivor = await CanonicalPayloadRepository(session, CONTEXT).get(row_id)
        assert survivor is not None
        assert survivor.payload == PAYLOAD


async def test_payloads_are_tenant_scoped(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await record_canonical_payload(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN,
            task_id=None,
            schema_version="1.0.0",
            payload=dict(PAYLOAD),
            actor_id="user:x",
        )
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await CanonicalPayloadRepository(session, other).get_for_run(RUN, "1.0.0") is None

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert "canonical_payloads" in RLS_PROTECTED_TABLES
