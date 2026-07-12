"""Reusable cross-tenant security matrix (TEN-011).

REQUIRED in CI. Every tenant-owned aggregate registers a case below; the
matrix then proves, for each one, that a repository scoped to tenant A:

- cannot ``get`` tenant B's row by ID,
- never lists/paginates tenant B's rows,
- counts only tenant A's rows,
- refuses to ``add`` an entity stamped for tenant B.

>>> HOW TO EXTEND (do this for every new tenant-owned resource) <<<
Add a ``TenantCase`` entry to ``TENANT_CASES`` with a factory that builds a
valid entity for a given organization. Nothing else is needed — the whole
matrix runs against the new aggregate automatically.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from soa_api.domain.credentials import ServiceCredential, ServiceCredentialRepository
from soa_api.domain.identity import Membership, MembershipRepository, MembershipStatus
from soa_api.domain.rbac import (
    Role,
    RoleAssignment,
    RoleAssignmentRepository,
    RoleRepository,
)
from soa_api.domain.tenancy import Workspace, WorkspaceRepository
from soa_db import Base, CursorRequest, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext, ScopedRepository, TenantMismatchError

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xAAA))
ORG_B = OrganizationContext(organization_id=uuid.UUID(int=0xBBB))


@dataclass(frozen=True)
class TenantCase:
    name: str
    repository: type[ScopedRepository[Any]]
    factory: Callable[[uuid.UUID, str], Any]


TENANT_CASES: list[TenantCase] = [
    TenantCase(
        name="workspace",
        repository=WorkspaceRepository,
        factory=lambda org, tag: Workspace(organization_id=org, name=tag, slug=f"ws-{tag}"),
    ),
    TenantCase(
        name="membership",
        repository=MembershipRepository,
        factory=lambda org, tag: Membership(
            organization_id=org,
            invited_email=f"{tag}@example.test",
            status=MembershipStatus.INVITED,
        ),
    ),
    TenantCase(
        name="role",
        repository=RoleRepository,
        factory=lambda org, tag: Role(
            organization_id=org, name=tag, slug=f"role-{tag}", permissions=["documents.read"]
        ),
    ),
    TenantCase(
        name="role_assignment",
        repository=RoleAssignmentRepository,
        factory=lambda org, tag: RoleAssignment(
            organization_id=org,
            membership_id=uuid.uuid5(uuid.NAMESPACE_URL, f"m-{tag}"),
            role_id=uuid.uuid5(uuid.NAMESPACE_URL, f"r-{tag}"),
        ),
    ),
    TenantCase(
        name="service_credential",
        repository=ServiceCredentialRepository,
        factory=lambda org, tag: ServiceCredential(
            organization_id=org,
            name=tag,
            key_prefix=uuid.uuid5(uuid.NAMESPACE_URL, tag).hex[:8],
            key_hash="0" * 64,
            scopes=["documents.read"],
            created_by="test",
        ),
    ),
]

CASE_IDS = [case.name for case in TENANT_CASES]


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/matrix.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = DatabaseSessions(engine)
    yield sessions
    await sessions.dispose()


@pytest.mark.parametrize("case", TENANT_CASES, ids=CASE_IDS)
async def test_cross_tenant_read_write_matrix(case: TenantCase, db: DatabaseSessions) -> None:
    # Seed one row per tenant.
    async with db.session_scope() as session:
        session.add(case.factory(ORG_A.organization_id, f"{case.name}-a"))
        session.add(case.factory(ORG_B.organization_id, f"{case.name}-b"))

    async with db.session_scope() as session:
        repo_a = case.repository(session, ORG_A)
        repo_b = case.repository(session, ORG_B)

        page_a = await repo_a.list_page(CursorRequest(limit=200))
        page_b = await repo_b.list_page(CursorRequest(limit=200))
        assert len(page_a.items) == 1, f"{case.name}: tenant A must see exactly its row"
        assert len(page_b.items) == 1, f"{case.name}: tenant B must see exactly its row"

        b_row_id = page_b.items[0].id
        assert (await repo_a.get(b_row_id)) is None, f"{case.name}: cross-tenant get leaked"
        assert (await repo_a.get(b_row_id, for_update=True)) is None

        assert await repo_a.count() == 1
        assert await repo_b.count() == 1

        with pytest.raises(TenantMismatchError):
            repo_a.add(case.factory(ORG_B.organization_id, f"{case.name}-forged"))
        session.expunge_all()


@pytest.mark.parametrize("case", TENANT_CASES, ids=CASE_IDS)
async def test_pagination_cursor_from_other_tenant_leaks_nothing(
    case: TenantCase, db: DatabaseSessions
) -> None:
    """A cursor minted while paging tenant B must not open tenant B's data
    when replayed against a tenant-A repository."""
    async with db.session_scope() as session:
        for i in range(3):
            session.add(case.factory(ORG_B.organization_id, f"{case.name}-b{i}"))

    async with db.session_scope() as session:
        repo_b = case.repository(session, ORG_B)
        page_b = await repo_b.list_page(CursorRequest(limit=1))
        assert page_b.next_cursor is not None

        from soa_db import decode_cursor

        stolen_cursor = decode_cursor(page_b.next_cursor)
        repo_a = case.repository(session, ORG_A)
        replayed = await repo_a.list_page(CursorRequest(limit=200, after=stolen_cursor))
        assert replayed.items == [], f"{case.name}: cursor replay leaked cross-tenant rows"
