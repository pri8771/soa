import pytest

from soa_api.auth.principal import (
    AuthMethod,
    PrincipalMappingError,
    principal_from_oidc_claims,
)


def test_maps_standard_oidc_claims() -> None:
    principal = principal_from_oidc_claims(
        {
            "sub": "auth0|abc123",
            "iss": "https://id.example.com/",
            "email": "reviewer@northstar.example",
            "name": "Riley Reviewer",
            "custom:tenant": "northstar",
        }
    )
    assert principal.subject == "auth0|abc123"
    assert principal.issuer == "https://id.example.com/"
    assert principal.email == "reviewer@northstar.example"
    assert principal.display_name == "Riley Reviewer"
    assert principal.auth_method is AuthMethod.OIDC
    assert principal.identity_key == "https://id.example.com/|auth0|abc123"
    assert principal.claims["custom:tenant"] == "northstar"


def test_falls_back_to_preferred_username() -> None:
    principal = principal_from_oidc_claims(
        {"sub": "s", "iss": "https://i", "preferred_username": "riley"}
    )
    assert principal.display_name == "riley"
    assert principal.email is None


def test_explicit_claim_overrides_take_precedence() -> None:
    principal = principal_from_oidc_claims(
        {"sub": "s", "iss": "https://i", "email": "std@x.com", "corp_mail": "real@corp.com"},
        email_claim="corp_mail",
    )
    assert principal.email == "real@corp.com"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://i"},
        {"sub": "", "iss": "https://i"},
        {"sub": 123, "iss": "https://i"},
        {"sub": "s"},
        {"sub": "s", "iss": ""},
    ],
)
def test_missing_required_claims_raise(claims: dict[str, object]) -> None:
    with pytest.raises(PrincipalMappingError):
        principal_from_oidc_claims(claims)
