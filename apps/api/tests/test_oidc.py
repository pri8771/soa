import json
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from soa_api.auth.errors import AuthenticationError
from soa_api.auth.oidc import OidcTokenValidator

ISSUER = "https://id.example.com/"
AUDIENCE = "soa-api"


def make_key(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private_key, jwk


KEY_A, JWK_A = make_key("key-a")
KEY_B, JWK_B = make_key("key-b")


def sign(
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_in: int = 300,
    algorithm: str = "RS256",
) -> str:
    now = int(time.time())
    claims = {
        "sub": f"user-{uuid.uuid4().hex[:8]}",
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "email": "person@example.com",
    }
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


def validator_with(*jwks_pages: dict[str, Any]) -> OidcTokenValidator:
    pages = list(jwks_pages)
    calls = {"count": 0}

    async def fetcher() -> dict[str, Any]:
        page = pages[min(calls["count"], len(pages) - 1)]
        calls["count"] += 1
        return page

    validator = OidcTokenValidator(issuer=ISSUER, audience=AUDIENCE, jwks_fetcher=fetcher)
    validator.fetch_calls = calls  # type: ignore[attr-defined]
    return validator


async def test_valid_token_produces_principal() -> None:
    validator = validator_with({"keys": [JWK_A]})
    principal = await validator.validate(sign(KEY_A, "key-a"))
    assert principal.issuer == ISSUER
    assert principal.email == "person@example.com"


async def test_expired_token_is_rejected() -> None:
    validator = validator_with({"keys": [JWK_A]})
    token = sign(KEY_A, "key-a", expires_in=-3600)
    with pytest.raises(AuthenticationError, match="expired"):
        await validator.validate(token)


async def test_wrong_audience_is_rejected() -> None:
    validator = validator_with({"keys": [JWK_A]})
    token = sign(KEY_A, "key-a", audience="another-api")
    with pytest.raises(AuthenticationError, match="audience"):
        await validator.validate(token)


async def test_wrong_issuer_is_rejected() -> None:
    validator = validator_with({"keys": [JWK_A]})
    token = sign(KEY_A, "key-a", issuer="https://evil.example.com/")
    with pytest.raises(AuthenticationError, match="issuer"):
        await validator.validate(token)


async def test_unknown_key_is_rejected() -> None:
    validator = validator_with({"keys": [JWK_A]})
    token = sign(KEY_B, "key-b")  # never appears in JWKS
    with pytest.raises(AuthenticationError, match="unknown key"):
        await validator.validate(token)


async def test_rotated_key_is_picked_up_via_refresh() -> None:
    # First fetch returns only key A; after rotation the endpoint serves both.
    validator = validator_with({"keys": [JWK_A]}, {"keys": [JWK_A, JWK_B]})
    assert (await validator.validate(sign(KEY_A, "key-a"))).issuer == ISSUER
    principal = await validator.validate(sign(KEY_B, "key-b"))
    assert principal.issuer == ISSUER
    assert validator.fetch_calls["count"] == 2  # type: ignore[attr-defined]


async def test_symmetric_algorithm_confusion_is_rejected() -> None:
    validator = validator_with({"keys": [JWK_A]})
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300},
        "shared-secret",
        algorithm="HS256",
        headers={"kid": "key-a"},
    )
    with pytest.raises(AuthenticationError, match="not permitted"):
        await validator.validate(forged)


def test_validator_refuses_symmetric_configuration() -> None:
    async def fetcher() -> dict[str, Any]:
        return {"keys": []}

    with pytest.raises(ValueError, match="not permitted"):
        OidcTokenValidator(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_fetcher=fetcher,
            allowed_algorithms=("HS256",),
        )
