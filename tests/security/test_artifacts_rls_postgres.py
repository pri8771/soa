"""Artifacts participate in RLS defense in depth (STO-003).

Same shape as the workspace RLS suite: unscoped ORM queries against
``artifacts`` must fail closed under the wrong (or no) tenant binding.
Runs only against real PostgreSQL via SOA_TEST_POSTGRES_URL.
"""

import os
import uuid

import pytest
from sqlalchemy import select

from soa_db import DatabaseSessions, create_database_engine
from soa_db.artifacts import Artifact, ArtifactKind, create_artifact
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; RLS tests require real PostgreSQL",
    ),
]

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()
SHA = "c" * 64


@pytest.fixture
async def db() -> DatabaseSessions:
    assert POSTGRES_URL is not None
    sessions = DatabaseSessions(create_database_engine(POSTGRES_URL))
    yield sessions
    for org in (ORG_A, ORG_B):
        async with sessions.session_scope() as session:
            await bind_tenant(session, org)
            rows = (
                (await session.execute(select(Artifact).where(Artifact.organization_id == org)))
                .scalars()
                .all()
            )
            for row in rows:
                await session.delete(row)
    await sessions.dispose()


async def seed(db: DatabaseSessions) -> None:
    for org in (ORG_A, ORG_B):
        async with db.session_scope() as session:
            await bind_tenant(session, org)
            await create_artifact(
                session,
                OrganizationContext(organization_id=org),
                document_id=uuid.uuid4(),
                kind=ArtifactKind.ORIGINAL,
                object_key=f"orgs/{org}/documents/x/original/{uuid.uuid4().hex}",
                sha256=SHA,
                size_bytes=10,
                content_type="application/pdf",
            )


async def test_unscoped_artifact_query_is_tenant_bound(db: DatabaseSessions) -> None:
    await seed(db)
    async with db.session_scope() as session:
        await bind_tenant(session, ORG_A)
        rows = (await session.execute(select(Artifact))).scalars().all()
        organizations = {row.organization_id for row in rows}
    assert ORG_B not in organizations
    assert ORG_A in organizations


async def test_unbound_transaction_reads_no_artifacts(db: DatabaseSessions) -> None:
    await seed(db)
    async with db.session_scope() as session:
        rows = (await session.execute(select(Artifact))).scalars().all()
    assert rows == []
