"""Allow/deny matrix for the central authorization service (TEN-007)."""

import uuid
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from soa_api.app import create_app
from soa_api.auth.authorization import (
    AuthorizationDeniedError,
    AuthorizationService,
    DenyReason,
)
from soa_api.auth.dependency import require_permission
from soa_api.auth.dev_identity import DEV_ISSUER
from soa_api.auth.principal import AuthMethod, Principal
from soa_api.domain.identity import Membership, MembershipStatus, User
from soa_api.domain.rbac import UnknownPermissionError, assign_role, seed_system_roles
from soa_api.domain.tenancy import Organization, OrganizationStatus
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext


def principal_for(user: User) -> Principal:
    issuer, _, subject = user.identity_key.partition("|")
    return Principal(subject=subject, issuer=issuer, auth_method=AuthMethod.OIDC)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/authz.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed_world(db: DatabaseSessions) -> dict[str, object]:
    """Two orgs; reviewer active in org A only; various broken memberships."""
    async with db.session_scope() as session:
        org_a = Organization(name="Org A", slug="org-a")
        org_b = Organization(name="Org B", slug="org-b")
        suspended_org = Organization(
            name="Suspended Org", slug="suspended-org", status=OrganizationStatus.SUSPENDED
        )
        session.add_all([org_a, org_b, suspended_org])
        await session.flush()

        ctx_a = OrganizationContext(organization_id=org_a.id)
        roles = await seed_system_roles(session, ctx_a, actor_id="system:test")
        reviewer_role = next(role for role in roles if role.slug == "reviewer")

        active_user = User(identity_key="https://idp|active", email="active@a.example")
        suspended_user = User(identity_key="https://idp|suspended", email="susp@a.example")
        outsider = User(identity_key="https://idp|outsider", email="out@b.example")
        no_role_user = User(identity_key="https://idp|norole", email="norole@a.example")
        session.add_all([active_user, suspended_user, outsider, no_role_user])
        await session.flush()

        active_membership = Membership(
            organization_id=org_a.id,
            user_id=active_user.id,
            invited_email=active_user.email,
            status=MembershipStatus.ACTIVE,
        )
        suspended_membership = Membership(
            organization_id=org_a.id,
            user_id=suspended_user.id,
            invited_email=suspended_user.email,
            status=MembershipStatus.SUSPENDED,
        )
        no_role_membership = Membership(
            organization_id=org_a.id,
            user_id=no_role_user.id,
            invited_email=no_role_user.email,
            status=MembershipStatus.ACTIVE,
        )
        outsider_membership_in_b = Membership(
            organization_id=org_b.id,
            user_id=outsider.id,
            invited_email=outsider.email,
            status=MembershipStatus.ACTIVE,
        )
        session.add_all(
            [
                active_membership,
                suspended_membership,
                no_role_membership,
                outsider_membership_in_b,
            ]
        )
        await session.flush()
        await assign_role(
            session,
            ctx_a,
            membership_id=active_membership.id,
            role_id=reviewer_role.id,
            actor_id="system:test",
        )
        return {
            "org_a_id": org_a.id,
            "active": principal_for(active_user),
            "suspended": principal_for(suspended_user),
            "outsider": principal_for(outsider),
            "no_role": principal_for(no_role_user),
        }


async def expect_denied(
    db: DatabaseSessions,
    principal: Principal,
    *,
    slug: str = "org-a",
    permission: str = "documents.review",
    reason: DenyReason,
) -> None:
    async with db.session_scope() as session:
        with pytest.raises(AuthorizationDeniedError) as excinfo:
            await AuthorizationService(session).authorize(
                principal, organization_slug=slug, required_permission=permission
            )
    assert excinfo.value.reason == reason


async def test_active_member_with_permission_is_allowed(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    async with db.session_scope() as session:
        ctx = await AuthorizationService(session).authorize(
            world["active"],  # type: ignore[arg-type]
            organization_slug="org-a",
            required_permission="documents.review",
        )
    assert ctx.organization.slug == "org-a"
    assert "documents.review" in ctx.permissions
    assert ctx.org_context.organization_id == world["org_a_id"]
    await db.dispose()


async def test_any_permission_accepts_one_grant_and_fails_closed_on_unknown_candidates(
    db: DatabaseSessions,
) -> None:
    world = await seed_world(db)
    async with db.session_scope() as session:
        ctx = await AuthorizationService(session).authorize_any(
            world["active"],  # type: ignore[arg-type]
            organization_slug="org-a",
            required_permissions=("roles.manage", "documents.review"),
        )
        assert "documents.review" in ctx.permissions

    async with db.session_scope() as session:
        with pytest.raises(UnknownPermissionError, match=r"documents\.obliterate"):
            await AuthorizationService(session).authorize_any(
                world["active"],  # type: ignore[arg-type]
                organization_slug="org-a",
                required_permissions=("documents.review", "documents.obliterate"),
            )
    await db.dispose()


async def test_member_without_permission_is_denied(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    # reviewer template lacks roles.manage
    await expect_denied(
        db,
        world["active"],  # type: ignore[arg-type]
        permission="roles.manage",
        reason=DenyReason.PERMISSION_MISSING,
    )
    await db.dispose()


async def test_membership_without_roles_is_denied(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    await expect_denied(
        db,
        world["no_role"],  # type: ignore[arg-type]
        reason=DenyReason.PERMISSION_MISSING,
    )
    await db.dispose()


async def test_suspended_membership_is_denied(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    await expect_denied(
        db,
        world["suspended"],  # type: ignore[arg-type]
        reason=DenyReason.MEMBERSHIP_INACTIVE,
    )
    await db.dispose()


async def test_cross_tenant_principal_is_denied(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    # outsider is an ACTIVE member of org B — must not reach org A
    await expect_denied(
        db,
        world["outsider"],  # type: ignore[arg-type]
        reason=DenyReason.NOT_A_MEMBER,
    )
    await db.dispose()


async def test_unknown_principal_is_denied(db: DatabaseSessions) -> None:
    await seed_world(db)
    stranger = Principal(subject="ghost", issuer="https://idp", auth_method=AuthMethod.OIDC)
    await expect_denied(db, stranger, reason=DenyReason.NOT_A_MEMBER)
    await db.dispose()


async def test_suspended_organization_is_denied_even_for_members(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    await expect_denied(
        db,
        world["active"],  # type: ignore[arg-type]
        slug="suspended-org",
        reason=DenyReason.ORGANIZATION_NOT_OPERATIONAL,
    )
    await db.dispose()


async def test_unknown_organization_is_denied(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    await expect_denied(
        db,
        world["active"],  # type: ignore[arg-type]
        slug="does-not-exist",
        reason=DenyReason.ORGANIZATION_NOT_FOUND,
    )
    await db.dispose()


async def test_unregistered_permission_fails_closed_before_data_access(
    db: DatabaseSessions,
) -> None:
    world = await seed_world(db)
    async with db.session_scope() as session:
        with pytest.raises(UnknownPermissionError):
            await AuthorizationService(session).authorize(
                world["active"],  # type: ignore[arg-type]
                organization_slug="org-a",
                required_permission="documents.obliterate",
            )
    await db.dispose()


async def test_verify_owned_rejects_foreign_resources(db: DatabaseSessions) -> None:
    world = await seed_world(db)
    async with db.session_scope() as session:
        ctx = await AuthorizationService(session).authorize(
            world["active"],  # type: ignore[arg-type]
            organization_slug="org-a",
            required_permission="documents.review",
        )

        class FakeResource:
            organization_id = uuid.uuid4()  # some other tenant

        with pytest.raises(AuthorizationDeniedError) as excinfo:
            ctx.verify_owned(FakeResource())
        assert excinfo.value.reason == DenyReason.RESOURCE_NOT_OWNED
    await db.dispose()


async def test_http_route_enforces_permission_via_dependency(
    db: DatabaseSessions, tmp_path: Path
) -> None:
    """End-to-end proof: dev-identity principal + require_permission guard."""
    async with db.session_scope() as session:
        org = Organization(name="Northstar", slug="northstar")
        session.add(org)
        await session.flush()
        ctx = OrganizationContext(organization_id=org.id)
        roles = await seed_system_roles(session, ctx, actor_id="system:test")
        reviewer_role = next(role for role in roles if role.slug == "reviewer")
        # Match the dev-identity principal for the seeded demo reviewer.
        from soa_fixtures import DEMO_TENANT

        demo_reviewer = next(u for u in DEMO_TENANT.users if u.data["role"] == "reviewer")
        user = User(
            identity_key=f"{DEV_ISSUER}|{demo_reviewer.id}",
            email=str(demo_reviewer.data["email"]),
        )
        session.add(user)
        await session.flush()
        membership = Membership(
            organization_id=org.id,
            user_id=user.id,
            invited_email=user.email,
            status=MembershipStatus.ACTIVE,
        )
        session.add(membership)
        await session.flush()
        await assign_role(
            session,
            ctx,
            membership_id=membership.id,
            role_id=reviewer_role.id,
            actor_id="system:test",
        )

    app = create_app(ApiSettings(environment=Environment.TEST), db=db)

    @app.get("/orgs/{organization_slug}/documents-check")
    async def documents_check(
        authorized=Depends(require_permission("documents.review")),  # noqa: B008
    ) -> dict[str, str]:
        return {"organization": authorized.organization.slug}

    @app.get("/orgs/{organization_slug}/admin-check")
    async def admin_check(
        authorized=Depends(require_permission("roles.manage")),  # noqa: B008
    ) -> dict[str, str]:
        return {"organization": authorized.organization.slug}

    client = TestClient(app)
    headers = {"X-Dev-User": "user:reviewer"}

    allowed = client.get("/orgs/northstar/documents-check", headers=headers)
    assert allowed.status_code == 200
    assert allowed.json() == {"organization": "northstar"}

    denied = client.get("/orgs/northstar/admin-check", headers=headers)
    assert denied.status_code == 403

    unauthenticated = client.get("/orgs/northstar/documents-check")
    assert unauthenticated.status_code == 401
    await db.dispose()
