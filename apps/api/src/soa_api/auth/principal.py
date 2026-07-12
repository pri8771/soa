"""Provider-neutral authentication principal (TEN-001, ADR-010).

Domain and application code depend on ``Principal`` only — never on a
vendor SDK or vendor claim names. Adapters (OIDC, development identity,
API keys) map their native representation into this contract.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AuthMethod(StrEnum):
    OIDC = "oidc"
    DEV = "dev"
    API_KEY = "api_key"


class PrincipalMappingError(Exception):
    """Raised when an identity token lacks the claims required to build a
    principal. The message is safe to log; it never contains token contents."""


@dataclass(frozen=True)
class Principal:
    subject: str
    issuer: str
    auth_method: AuthMethod
    email: str | None = None
    display_name: str | None = None
    # Retained for membership lookup and adapter-specific policy; application
    # code must not read vendor claim names from here directly.
    claims: Mapping[str, Any] = field(default_factory=dict)

    @property
    def identity_key(self) -> str:
        """Stable identity across logins: issuer + subject."""
        return f"{self.issuer}|{self.subject}"


# Standard OIDC claim names with common fallbacks. Vendor adapters may pass
# an explicit email/display-name claim override instead of adding vendor
# names here.
_EMAIL_CLAIMS = ("email",)
_NAME_CLAIMS = ("name", "preferred_username", "nickname")


def principal_from_oidc_claims(
    claims: Mapping[str, Any],
    *,
    email_claim: str | None = None,
    name_claim: str | None = None,
) -> Principal:
    subject = claims.get("sub")
    issuer = claims.get("iss")
    if not subject or not isinstance(subject, str):
        raise PrincipalMappingError("token is missing a usable 'sub' claim")
    if not issuer or not isinstance(issuer, str):
        raise PrincipalMappingError("token is missing a usable 'iss' claim")

    email: str | None = None
    for claim in (email_claim,) if email_claim else _EMAIL_CLAIMS:
        value = claims.get(claim) if claim else None
        if isinstance(value, str) and value:
            email = value
            break

    display_name: str | None = None
    for claim in (name_claim,) if name_claim else _NAME_CLAIMS:
        value = claims.get(claim) if claim else None
        if isinstance(value, str) and value:
            display_name = value
            break

    return Principal(
        subject=subject,
        issuer=issuer,
        auth_method=AuthMethod.OIDC,
        email=email,
        display_name=display_name,
        claims=dict(claims),
    )
