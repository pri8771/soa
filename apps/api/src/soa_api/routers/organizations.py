"""Organization, membership, invitation, and role endpoints (TEN-008)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import CurrentPrincipal, require_permission
from soa_api.dependencies import DbSession
from soa_api.domain.identity import (
    InvalidMembershipTransitionError,
    MembershipRepository,
    MembershipStatus,
)
from soa_api.domain.rbac import (
    Role,
    RoleAssignmentRepository,
    RoleRepository,
    UnknownPermissionError,
    assign_role,
    create_custom_role,
    revoke_role,
    update_role_permissions,
)
from soa_api.domain.tenancy import Organization, OrganizationRepository
from soa_api.services import tenancy_service
from soa_db import CursorRequest, InvalidCursorError, decode_cursor
from soa_db.mixins import VersionConflictError
from soa_db.tenant_guard import bind_user

router = APIRouter(tags=["organizations"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")


class OrganizationResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    version: int

    @classmethod
    def from_model(cls, organization: Organization) -> "OrganizationResponse":
        return cls(
            id=str(organization.id),
            name=organization.name,
            slug=organization.slug,
            status=organization.status,
            version=organization.version,
        )


class MemberResponse(BaseModel):
    membership_id: str
    user_id: str | None
    invited_email: str
    status: str
    version: int


class MembersPageResponse(BaseModel):
    items: list[MemberResponse]
    has_more: bool
    next_cursor: str | None


class InviteRequest(BaseModel):
    email: EmailStr


class InviteResponse(BaseModel):
    membership_id: str
    email: str
    status: str
    created: bool


class AcceptInvitationRequest(BaseModel):
    organization_slug: str


class MembershipStatusRequest(BaseModel):
    status: MembershipStatus


class RoleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    permissions: list[str]


class RoleResponse(BaseModel):
    id: str
    name: str
    slug: str
    is_system: bool
    permissions: list[str]
    version: int

    @classmethod
    def from_model(cls, role: Role) -> "RoleResponse":
        return cls(
            id=str(role.id),
            name=role.name,
            slug=role.slug,
            is_system=role.is_system,
            permissions=list(role.permissions),
            version=role.version,
        )


class RolePermissionsUpdateRequest(BaseModel):
    permissions: list[str]


class RoleAssignRequest(BaseModel):
    role_slug: str


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreateRequest,
    principal: CurrentPrincipal,
    session: DbSession,
) -> OrganizationResponse:
    try:
        created = await tenancy_service.create_organization(
            session, principal, name=body.name, slug=body.slug
        )
    except tenancy_service.SlugTakenError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization slug {body.slug!r} is already taken.",
        ) from None
    return OrganizationResponse.from_model(created.organization)


@router.get("/organizations")
async def list_my_organizations(
    principal: CurrentPrincipal,
    session: DbSession,
) -> list[OrganizationResponse]:
    user = await tenancy_service.ensure_user(session, principal)
    await bind_user(session, user.id)  # RLS: self-scoped membership reads
    memberships = await tenancy_service.list_memberships_for_user(session, user.id)
    org_repo = OrganizationRepository(session)
    organizations: list[OrganizationResponse] = []
    for membership in memberships:
        if not membership.grants_access:
            continue
        organization = await org_repo.get(membership.organization_id)
        if organization is not None:
            organizations.append(OrganizationResponse.from_model(organization))
    return organizations


@router.get("/orgs/{organization_slug}")
async def get_organization(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("organization.read"))],
) -> OrganizationResponse:
    return OrganizationResponse.from_model(authorized.organization)


# ---------------------------------------------------------------------------
# Members and invitations
# ---------------------------------------------------------------------------


@router.get("/orgs/{organization_slug}/members")
async def list_members(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("members.read"))],
    session: DbSession,
    limit: int = 50,
    cursor: str | None = None,
) -> MembersPageResponse:
    try:
        after = decode_cursor(cursor) if cursor else None
        request = CursorRequest(limit=limit, after=after)
    except (InvalidCursorError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    page = await MembershipRepository(session, authorized.org_context).list_page(request)
    return MembersPageResponse(
        items=[
            MemberResponse(
                membership_id=str(m.id),
                user_id=str(m.user_id) if m.user_id else None,
                invited_email=m.invited_email,
                status=m.status,
                version=m.version,
            )
            for m in page.items
        ],
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.post("/orgs/{organization_slug}/invitations", status_code=status.HTTP_201_CREATED)
async def invite_member(
    body: InviteRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("members.manage"))],
    session: DbSession,
) -> InviteResponse:
    membership, created = await tenancy_service.invite_member(
        session,
        authorized.org_context,
        email=body.email,
        actor_id=f"user:{authorized.membership.user_id}",
    )
    return InviteResponse(
        membership_id=str(membership.id),
        email=membership.invited_email,
        status=membership.status,
        created=created,
    )


@router.post("/invitations/accept")
async def accept_invitation(
    body: AcceptInvitationRequest,
    principal: CurrentPrincipal,
    session: DbSession,
) -> MemberResponse:
    try:
        membership = await tenancy_service.accept_invitation(
            session, principal, organization_slug=body.organization_slug
        )
    except tenancy_service.NoInvitationError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No pending invitation for this organization.",
        ) from None
    await session.flush()  # apply the version increment before serializing
    return MemberResponse(
        membership_id=str(membership.id),
        user_id=str(membership.user_id) if membership.user_id else None,
        invited_email=membership.invited_email,
        status=membership.status,
        version=membership.version,
    )


@router.patch("/orgs/{organization_slug}/members/{membership_id}")
async def change_membership_status(
    membership_id: uuid.UUID,
    body: MembershipStatusRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("members.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> MemberResponse:
    membership = await MembershipRepository(session, authorized.org_context).get(membership_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    try:
        if if_match is not None:
            membership.expect_version(if_match)
        membership = await tenancy_service.change_membership_status(
            session,
            authorized.org_context,
            membership=membership,
            new_status=body.status,
            actor_id=f"user:{authorized.membership.user_id}",
        )
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except InvalidMembershipTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await session.flush()
    return MemberResponse(
        membership_id=str(membership.id),
        user_id=str(membership.user_id) if membership.user_id else None,
        invited_email=membership.invited_email,
        status=membership.status,
        version=membership.version,
    )


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@router.get("/orgs/{organization_slug}/roles")
async def list_roles(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("roles.read"))],
    session: DbSession,
) -> list[RoleResponse]:
    page = await RoleRepository(session, authorized.org_context).list_page(CursorRequest(limit=200))
    return [RoleResponse.from_model(role) for role in page.items]


@router.post("/orgs/{organization_slug}/roles", status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("roles.manage"))],
    session: DbSession,
) -> RoleResponse:
    try:
        role = await create_custom_role(
            session,
            authorized.org_context,
            name=body.name,
            slug=body.slug,
            permissions=body.permissions,
            actor_id=f"user:{authorized.membership.user_id}",
        )
    except UnknownPermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return RoleResponse.from_model(role)


@router.put("/orgs/{organization_slug}/roles/{role_slug}")
async def update_role(
    role_slug: str,
    body: RolePermissionsUpdateRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("roles.manage"))],
    session: DbSession,
    if_match: Annotated[int | None, Header(alias="If-Match")] = None,
) -> RoleResponse:
    """Replace a role's permission set so newly-added permissions reach an
    existing org without hand SQL. System roles stay ``is_system`` (this is
    exactly the drift-correction path); permissions fail closed against the
    registry and internal permissions remain non-grantable."""
    role = await RoleRepository(session, authorized.org_context).get_by_slug(role_slug)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found.")
    try:
        if if_match is not None:
            role.expect_version(if_match)
        role = await update_role_permissions(
            session,
            authorized.org_context,
            role=role,
            permissions=body.permissions,
            actor_id=f"user:{authorized.membership.user_id}",
        )
    except VersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except UnknownPermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return RoleResponse.from_model(role)


@router.post(
    "/orgs/{organization_slug}/members/{membership_id}/roles",
    status_code=status.HTTP_201_CREATED,
)
async def assign_member_role(
    membership_id: uuid.UUID,
    body: RoleAssignRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("roles.manage"))],
    session: DbSession,
) -> dict[str, str]:
    membership = await MembershipRepository(session, authorized.org_context).get(membership_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    role = await RoleRepository(session, authorized.org_context).get_by_slug(body.role_slug)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found.")
    await assign_role(
        session,
        authorized.org_context,
        membership_id=membership.id,
        role_id=role.id,
        actor_id=f"user:{authorized.membership.user_id}",
    )
    return {"membership_id": str(membership.id), "role_slug": role.slug}


@router.delete("/orgs/{organization_slug}/members/{membership_id}/roles/{role_slug}")
async def revoke_member_role(
    membership_id: uuid.UUID,
    role_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("roles.manage"))],
    session: DbSession,
) -> dict[str, str]:
    role = await RoleRepository(session, authorized.org_context).get_by_slug(role_slug)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found.")
    assignments = await RoleAssignmentRepository(
        session, authorized.org_context
    ).list_for_membership(membership_id)
    assignment = next((a for a in assignments if a.role_id == role.id), None)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found.")
    await revoke_role(
        session,
        authorized.org_context,
        assignment=assignment,
        actor_id=f"user:{authorized.membership.user_id}",
    )
    return {"membership_id": str(membership_id), "role_slug": role_slug, "revoked": "true"}
