"""Database-level tenant isolation tests (TEN-010).

These run ONLY against real PostgreSQL (RLS does not exist on SQLite).
Set ``SOA_TEST_POSTGRES_URL`` (asyncpg URL) to enable; CI provides it in
the migrations job after applying migrations to head.

The point of every test here: a *raw ORM query with no repository and no
organization filter* — the accidental-unscoped-query case — must return
nothing from another tenant once RLS is active.
"""

import os
import uuid

import pytest
from sqlalchemy import select

from soa_api.domain.tenancy import Workspace
from soa_db import DatabaseSessions, create_database_engine
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


@pytest.fixture
async def db() -> DatabaseSessions:
    assert POSTGRES_URL is not None
    sessions = DatabaseSessions(create_database_engine(POSTGRES_URL))
    yield sessions
    # Clean up rows created by this test run (bind each tenant to delete).
    for org in (ORG_A, ORG_B):
        async with sessions.session_scope() as session:
            await bind_tenant(session, org)
            for row in (
                (await session.execute(select(Workspace).where(Workspace.organization_id == org)))
                .scalars()
                .all()
            ):
                await session.delete(row)
    await sessions.dispose()


async def seed_workspaces(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await bind_tenant(session, ORG_A)
        session.add(Workspace(organization_id=ORG_A, name="A Space", slug=f"a-{ORG_A.hex[:8]}"))
    async with db.session_scope() as session:
        await bind_tenant(session, ORG_B)
        session.add(Workspace(organization_id=ORG_B, name="B Space", slug=f"b-{ORG_B.hex[:8]}"))


async def test_unscoped_query_under_wrong_tenant_reads_nothing(db: DatabaseSessions) -> None:
    await seed_workspaces(db)
    async with db.session_scope() as session:
        await bind_tenant(session, ORG_A)
        # Deliberately unscoped ORM query — the accident RLS must absorb.
        rows = (await session.execute(select(Workspace))).scalars().all()
        organizations = {row.organization_id for row in rows}
    assert ORG_B not in organizations, "RLS must hide other tenants from unscoped queries"
    assert ORG_A in organizations


async def test_unbound_transaction_reads_nothing(db: DatabaseSessions) -> None:
    await seed_workspaces(db)
    async with db.session_scope() as session:
        rows = (await session.execute(select(Workspace))).scalars().all()
    assert rows == [], "a transaction without tenant binding must fail closed"


async def test_unbound_transaction_cannot_insert(db: DatabaseSessions) -> None:
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with pytest.raises((ProgrammingError, DBAPIError)):
        async with db.session_scope() as session:
            session.add(
                Workspace(organization_id=ORG_A, name="Sneaky", slug=f"s-{uuid.uuid4().hex[:8]}")
            )


async def test_cross_tenant_insert_is_rejected(db: DatabaseSessions) -> None:
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with pytest.raises((ProgrammingError, DBAPIError)):
        async with db.session_scope() as session:
            await bind_tenant(session, ORG_A)
            # WITH CHECK must reject writing a row stamped for another tenant.
            session.add(
                Workspace(organization_id=ORG_B, name="Forged", slug=f"f-{uuid.uuid4().hex[:8]}")
            )
