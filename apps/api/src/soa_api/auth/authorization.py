"""Central authorization service (TEN-007, ADR-011).

Every protected use case — HTTP handler or worker job — resolves an
``AuthorizedContext`` through this service. The context is the ONLY
sanctioned way to obtain an ``OrganizationContext`` for repositories, so
handlers cannot accidentally query tenant data without passing the checks:

1. Organization exists and is operational (not suspended/closed).
2. The principal maps to a user with an ACTIVE membership in it.
3. The required permission is registered (unknown -> fail closed) and held.
4. Resource ownership: ``verify_owned`` re-checks any loaded entity.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.auth.principal import Principal
from soa_api.domain.identity import Membership, MembershipRepository, User, UserRepository
from soa_api.domain.rbac import (
    PERMISSION_REGISTRY,
    UnknownPermissionError,
    permissions_for_membership,
)
from soa_api.domain.tenancy import Organization, OrganizationRepository
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant, bind_user


class DenyReason(StrEnum):
    ORGANIZATION_NOT_FOUND = "organization_not_found"
    ORGANIZATION_NOT_OPERATIONAL = "organization_not_operational"
    NOT_A_MEMBER = "not_a_member"
    MEMBERSHIP_INACTIVE = "membership_inactive"
    PERMISSION_MISSING = "permission_missing"
    RESOURCE_NOT_OWNED = "resource_not_owned"


class AuthorizationDeniedError(Exception):
    """Denial with a stable reason code. The message is safe for clients."""

    def __init__(self, reason: DenyReason) -> None:
        self.reason = reason
        super().__init__(f"authorization denied: {reason.value}")


@dataclass(frozen=True)
class AuthorizedContext:
    principal: Principal
    organization: Organization
    membership: Membership
    permissions: frozenset[str]

    @property
    def org_context(self) -> OrganizationContext:
        return OrganizationContext(organization_id=self.organization.id)

    def verify_owned(self, entity: object) -> None:
        """Re-check that a loaded resource belongs to this tenant. Defense in
        depth for entities obtained outside a scoped repository."""
        owner = getattr(entity, "organization_id", None)
        if owner != self.organization.id:
            raise AuthorizationDeniedError(DenyReason.RESOURCE_NOT_OWNED)


class AuthorizationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authorize(
        self,
        principal: Principal,
        *,
        organization_slug: str,
        required_permission: str,
    ) -> AuthorizedContext:
        # Fail closed on unregistered permissions BEFORE touching data.
        if required_permission not in PERMISSION_REGISTRY:
            raise UnknownPermissionError(required_permission)

        organization = await OrganizationRepository(self._session).get_by_slug(organization_slug)
        if organization is None:
            raise AuthorizationDeniedError(DenyReason.ORGANIZATION_NOT_FOUND)
        if not organization.is_operational:
            raise AuthorizationDeniedError(DenyReason.ORGANIZATION_NOT_OPERATIONAL)

        user = await self._resolve_user(principal)
        if user is None:
            raise AuthorizationDeniedError(DenyReason.NOT_A_MEMBER)

        # RLS ordering matters on real PostgreSQL: memberships/roles carry
        # FORCED policies, so the membership lookup runs under the caller's
        # user binding (user_self_access policy), and the tenant binding is
        # applied only AFTER the active membership is confirmed — before the
        # role/permission reads that need it.
        await bind_user(self._session, user.id)

        context = OrganizationContext(organization_id=organization.id)
        membership = await MembershipRepository(self._session, context).get_for_user(user.id)
        if membership is None:
            raise AuthorizationDeniedError(DenyReason.NOT_A_MEMBER)
        if not membership.grants_access:
            raise AuthorizationDeniedError(DenyReason.MEMBERSHIP_INACTIVE)

        await bind_tenant(self._session, organization.id)
        permissions = await permissions_for_membership(self._session, context, membership.id)
        if required_permission not in permissions:
            raise AuthorizationDeniedError(DenyReason.PERMISSION_MISSING)

        return AuthorizedContext(
            principal=principal,
            organization=organization,
            membership=membership,
            permissions=permissions,
        )

    async def _resolve_user(self, principal: Principal) -> User | None:
        return await UserRepository(self._session).get_by_identity_key(principal.identity_key)
