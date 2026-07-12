"""FastAPI dependency resolving the current principal.

Resolution order:
1. Development identity (``X-Dev-User``) — only when auth_dev_mode is on,
   which production settings forbid.
2. OIDC bearer token — when a validator is configured on the container.
3. Otherwise 401 with the structured error envelope.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from soa_api.auth.dev_identity import DEV_USER_HEADER, authenticate_dev_user
from soa_api.auth.errors import AuthenticationError
from soa_api.auth.principal import Principal
from soa_api.dependencies import Dependencies, get_dependencies


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
