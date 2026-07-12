"""FastAPI dependency resolving the current principal.

Resolution order:
1. Development identity (``X-Dev-User``) — only when auth_dev_mode is on,
   which production settings forbid.
2. OIDC bearer token — when a validator is configured on the container.
3. Otherwise 401 with the structured error envelope.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from soa_api.auth.authorization import (
    AuthorizationDeniedError,
    AuthorizationService,
    AuthorizedContext,
    DenyReason,
)
from soa_api.auth.dev_identity import DEV_USER_HEADER, authenticate_dev_user
from soa_api.auth.errors import AuthenticationError
from soa_api.auth.principal import Principal
from soa_api.dependencies import DbSession, Dependencies, get_dependencies
from soa_db.tenant_guard import bind_tenant


async def get_current_principal(
    request: Request,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> Principal:
    settings = deps.settings

    if settings.auth_dev_mode and settings.is_development_like:
        dev_header = request.headers.get(DEV_USER_HEADER)
        if dev_header is not None:
            try:
                return authenticate_dev_user(dev_header)
            except AuthenticationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
                ) from None

    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer ") and deps.oidc_validator is not None:
        token = authorization.removeprefix("Bearer ").strip()
        try:
            return await deps.oidc_validator.validate(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
    )


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]

_DENY_STATUS: dict[DenyReason, int] = {
    # Organization existence is not information non-members may learn.
    DenyReason.ORGANIZATION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    DenyReason.ORGANIZATION_NOT_OPERATIONAL: status.HTTP_403_FORBIDDEN,
    DenyReason.NOT_A_MEMBER: status.HTTP_404_NOT_FOUND,
    DenyReason.MEMBERSHIP_INACTIVE: status.HTTP_403_FORBIDDEN,
    DenyReason.PERMISSION_MISSING: status.HTTP_403_FORBIDDEN,
    DenyReason.RESOURCE_NOT_OWNED: status.HTTP_404_NOT_FOUND,
}


def require_permission(
    permission: str,
) -> Callable[..., Awaitable[AuthorizedContext]]:
    """Dependency factory: routes declare the permission they need and
    receive an AuthorizedContext — the only sanctioned way to reach tenant
    data. Route path must include ``{organization_slug}``."""

    async def dependency(
        organization_slug: str,
        principal: CurrentPrincipal,
        session: DbSession,
    ) -> AuthorizedContext:
        service = AuthorizationService(session)
        try:
            authorized = await service.authorize(
                principal,
                organization_slug=organization_slug,
                required_permission=permission,
            )
        except AuthorizationDeniedError as exc:
            raise HTTPException(
                status_code=_DENY_STATUS[exc.reason],
                detail=str(exc),
            ) from None
        # RLS defense in depth: bind the transaction to the authorized
        # tenant so unscoped queries fail closed at the database (TEN-010).
        await bind_tenant(session, authorized.organization.id)
        return authorized

    return dependency
