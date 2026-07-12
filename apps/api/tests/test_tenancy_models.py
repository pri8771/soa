import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_api.domain.tenancy import (
    Organization,
    OrganizationRepository,
    OrganizationStatus,
    Workspace,
    WorkspaceRepository,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/tenancy.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def create_org(sessions: DatabaseSessions, slug: str = "northstar") -> uuid.UUID:
    async with sessions.session_scope() as session:
        org = OrganizationRepository(session).add(
            Organization(name="Northstar Distribution", slug=slug)
        )
    return org.id


async def test_organization_crud_and_defaults(sessions: DatabaseSessions) -> None:
    org_id = await create_org(sessions)
    async with sessions.session_scope() as session:
        repo = OrganizationRepository(session)
        loaded = await repo.get(org_id)
        by_slug = await repo.get_by_slug("northstar")
    assert loaded is not None and by_slug is not None
    assert loaded.id == by_slug.id
    assert loaded.status == OrganizationStatus.ACTIVE
    assert loaded.is_operational
    assert loaded.locale == "en-GB"
    await sessions.dispose()


async def test_slug_uniqueness_is_global(sessions: DatabaseSessions) -> None:
    await create_org(sessions, "acme")
    with pytest.raises(IntegrityError):
        async with sessions.session_scope() as session:
            OrganizationRepository(session).add(Organization(name="Other Acme", slug="acme"))
    await sessions.dispose()


async def test_suspended_and_closed_are_not_operational(sessions: DatabaseSessions) -> None:
    org_id = await create_org(sessions)
    async with sessions.session_scope() as session:
        org = await OrganizationRepository(session).get(org_id)
        assert org is not None
        org.status = OrganizationStatus.SUSPENDED
    async with sessions.session_scope() as session:
        org = await OrganizationRepository(session).get(org_id)
        assert org is not None
        assert not org.is_operational
        org.status = OrganizationStatus.CLOSED
    async with sessions.session_scope() as session:
        org = await OrganizationRepository(session).get(org_id)
        assert org is not None
        assert not org.is_operational
    await sessions.dispose()


async def test_workspaces_are_tenant_scoped(sessions: DatabaseSessions) -> None:
    org_a = await create_org(sessions, "org-a")
    org_b = await create_org(sessions, "org-b")
    async with sessions.session_scope() as session:
        repo_a = WorkspaceRepository(session, OrganizationContext(organization_id=org_a))
        workspace = repo_a.add(Workspace(name="Europe", slug="europe"))
    workspace_id = workspace.id

    async with sessions.session_scope() as session:
        repo_b = WorkspaceRepository(session, OrganizationContext(organization_id=org_b))
        assert (await repo_b.get(workspace_id)) is None, "cross-tenant workspace read"
        assert (await repo_b.get_by_slug("europe")) is None
        repo_a = WorkspaceRepository(session, OrganizationContext(organization_id=org_a))
        assert (await repo_a.get(workspace_id)) is not None
    await sessions.dispose()


async def test_workspace_slug_unique_per_org_but_reusable_across_orgs(
    sessions: DatabaseSessions,
) -> None:
    org_a = await create_org(sessions, "org-a")
    org_b = await create_org(sessions, "org-b")
    async with sessions.session_scope() as session:
        WorkspaceRepository(session, OrganizationContext(organization_id=org_a)).add(
            Workspace(name="Europe", slug="europe")
        )
    async with sessions.session_scope() as session:
        WorkspaceRepository(session, OrganizationContext(organization_id=org_b)).add(
            Workspace(name="Europe", slug="europe")
        )
    with pytest.raises(IntegrityError):
        async with sessions.session_scope() as session:
            WorkspaceRepository(session, OrganizationContext(organization_id=org_a)).add(
                Workspace(name="Europe Again", slug="europe")
            )
    await sessions.dispose()
