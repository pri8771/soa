import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_api.domain.rbac import (
    PERMISSION_REGISTRY,
    SYSTEM_ROLE_TEMPLATES,
    RoleAssignmentRepository,
    RoleRepository,
    UnknownPermissionError,
    assign_role,
    create_custom_role,
    permissions_for_membership,
    revoke_role,
    seed_system_roles,
    validate_permissions,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.repository import OrganizationContext

ORG = OrganizationContext(organization_id=uuid.UUID(int=0xC1))
MEMBERSHIP_ID = uuid.UUID(int=0xD1)


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/rbac.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def test_unknown_permission_fails_closed() -> None:
    with pytest.raises(UnknownPermissionError, match=r"documents\.delete_everything"):
        validate_permissions(["documents.read", "documents.delete_everything"])


def test_system_templates_only_use_registered_permissions() -> None:
    for slug, permissions in SYSTEM_ROLE_TEMPLATES.items():
        assert permissions <= PERMISSION_REGISTRY, f"template {slug} has unknown permissions"


async def test_seed_creates_all_system_roles_idempotently(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        created = await seed_system_roles(session, ORG, actor_id="system:bootstrap")
        assert len(created) == len(SYSTEM_ROLE_TEMPLATES)
    async with sessions.session_scope() as session:
        again = await seed_system_roles(session, ORG, actor_id="system:bootstrap")
        assert again == []
        repo = RoleRepository(session, ORG)
        reviewer = await repo.get_by_slug("reviewer")
        assert reviewer is not None and reviewer.is_system
        assert "documents.review" in reviewer.permissions
        assert "roles.manage" not in reviewer.permissions
    await sessions.dispose()


async def test_custom_role_with_unknown_permission_is_rejected(
    sessions: DatabaseSessions,
) -> None:
    with pytest.raises(UnknownPermissionError):
        async with sessions.session_scope() as session:
            await create_custom_role(
                session,
                ORG,
                name="Chaos",
                slug="chaos",
                permissions=["everything.always"],
                actor_id="user:admin",
            )
    await sessions.dispose()


async def test_role_lifecycle_is_audited(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        role = await create_custom_role(
            session,
            ORG,
            name="Exports Only",
            slug="exports-only",
            permissions=["integrations.read", "integrations.replay"],
            actor_id="user:admin",
        )
        assignment = await assign_role(
            session, ORG, membership_id=MEMBERSHIP_ID, role_id=role.id, actor_id="user:admin"
        )
        await revoke_role(session, ORG, assignment=assignment, actor_id="user:admin")
    async with sessions.session_scope() as session:
        actions = [
            row.action
            for row in (await session.execute(select(AuditEvent).order_by(AuditEvent.occurred_at)))
            .scalars()
            .all()
        ]
    assert actions == ["role.created", "role.assigned", "role.revoked"]
    await sessions.dispose()


async def test_permissions_union_across_roles(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        reader = await create_custom_role(
            session,
            ORG,
            name="Reader",
            slug="reader",
            permissions=["documents.read"],
            actor_id="user:admin",
        )
        approver = await create_custom_role(
            session,
            ORG,
            name="Approver",
            slug="approver",
            permissions=["documents.approve", "documents.read"],
            actor_id="user:admin",
        )
        await assign_role(
            session, ORG, membership_id=MEMBERSHIP_ID, role_id=reader.id, actor_id="user:admin"
        )
        await assign_role(
            session, ORG, membership_id=MEMBERSHIP_ID, role_id=approver.id, actor_id="user:admin"
        )
    async with sessions.session_scope() as session:
        permissions = await permissions_for_membership(session, ORG, MEMBERSHIP_ID)
    assert permissions == frozenset({"documents.read", "documents.approve"})
    await sessions.dispose()


async def test_membership_without_roles_has_no_permissions(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        permissions = await permissions_for_membership(session, ORG, uuid.uuid4())
    assert permissions == frozenset()
    await sessions.dispose()


async def test_roles_are_tenant_scoped(sessions: DatabaseSessions) -> None:
    other = OrganizationContext(organization_id=uuid.UUID(int=0xC2))
    async with sessions.session_scope() as session:
        role = await create_custom_role(
            session,
            ORG,
            name="Ours",
            slug="ours",
            permissions=["documents.read"],
            actor_id="user:admin",
        )
    async with sessions.session_scope() as session:
        assert (await RoleRepository(session, other).get(role.id)) is None
        assert (await RoleRepository(session, other).get_by_slug("ours")) is None
        assignments = await RoleAssignmentRepository(session, other).list_for_membership(
            MEMBERSHIP_ID
        )
        assert list(assignments) == []
    await sessions.dispose()
