"""Current-identity endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from soa_api.auth.dependency import CurrentPrincipal
from soa_api.dependencies import DbSession, Dependencies, get_dependencies
from soa_api.domain.rbac import permissions_for_membership
from soa_api.domain.tenancy import OrganizationRepository
from soa_api.services.tenancy_service import ensure_user, list_memberships_for_user
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant, bind_user

router = APIRouter(tags=["me"])


class MembershipSummary(BaseModel):
    membership_id: str
    organization_id: str
    organization_slug: str
    organization_name: str
    organization_status: str
    status: str
    permissions: list[str]


class MeResponse(BaseModel):
    user_id: str
    email: str
    display_name: str | None
    auth_method: str
    dev_session: bool
    memberships: list[MembershipSummary]


@router.get("/me")
async def me(
    request: Request,
    principal: CurrentPrincipal,
    session: DbSession,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> MeResponse:
    # Abuse control (SEC-003): identity resolution is the session
    # bootstrap — the closest thing to a local login (real login lives
    # at the IdP, OPEN-002) — so it is capped per client address.
    await deps.rate_limiter.enforce(
        "identity",
        request.client.host if request.client else "unknown",
        deps.settings.rate_limit_identity_per_minute,
    )
    user = await ensure_user(session, principal)
    await bind_user(session, user.id)  # RLS: self-scoped membership reads
    memberships = await list_memberships_for_user(session, user.id)
    org_repo = OrganizationRepository(session)

    summaries: list[MembershipSummary] = []
    for membership in memberships:
        organization = await org_repo.get(membership.organization_id)
        if organization is None:
            continue
        permissions: list[str] = []
        if membership.grants_access:
            # Permission resolution reads tenant-scoped role tables.
            await bind_tenant(session, membership.organization_id)
            context = OrganizationContext(organization_id=membership.organization_id)
            permissions = sorted(await permissions_for_membership(session, context, membership.id))
        summaries.append(
            MembershipSummary(
                membership_id=str(membership.id),
                organization_id=str(membership.organization_id),
                organization_slug=organization.slug,
                organization_name=organization.name,
                organization_status=organization.status,
                status=membership.status,
                permissions=permissions,
            )
        )

    return MeResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        auth_method=principal.auth_method.value,
        dev_session=principal.auth_method.value == "dev",
        memberships=summaries,
    )
