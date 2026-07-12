"""Current-identity endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from soa_api.auth.dependency import CurrentPrincipal
from soa_api.dependencies import DbSession
from soa_api.services.tenancy_service import ensure_user, list_memberships_for_user

router = APIRouter(tags=["me"])


class MembershipSummary(BaseModel):
    membership_id: str
    organization_id: str
    status: str


class MeResponse(BaseModel):
    user_id: str
    email: str
    display_name: str | None
    auth_method: str
    dev_session: bool
    memberships: list[MembershipSummary]


@router.get("/me")
async def me(principal: CurrentPrincipal, session: DbSession) -> MeResponse:
    user = await ensure_user(session, principal)
    memberships = await list_memberships_for_user(session, user.id)
    return MeResponse(
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        auth_method=principal.auth_method.value,
        dev_session=principal.auth_method.value == "dev",
        memberships=[
            MembershipSummary(
                membership_id=str(m.id),
                organization_id=str(m.organization_id),
                status=m.status,
            )
            for m in memberships
        ],
    )
