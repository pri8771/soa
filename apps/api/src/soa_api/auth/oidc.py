"""OIDC JWT validation adapter (TEN-003, ADR-010).

Validates issuer, audience, signature, and time claims with bounded clock
skew. Signing keys come from a JWKS cache that refreshes once when an
unknown ``kid`` appears (key rotation) — never on every request.

Security posture:
- Asymmetric algorithms only (RS256/ES256 by default). ``none`` and HMAC
  are rejected outright, closing the algorithm-confusion class of attacks.
- Every failure raises ``AuthenticationError`` with a safe message.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from soa_api.auth.errors import AuthenticationError
from soa_api.auth.principal import Principal, principal_from_oidc_claims

logger = logging.getLogger(__name__)

JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


def httpx_jwks_fetcher(url: str, *, timeout_seconds: float = 5.0) -> JwksFetcher:
    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data

    return fetch


DEFAULT_ALLOWED_ALGORITHMS = ("RS256", "ES256")
DEFAULT_LEEWAY_SECONDS = 30


class OidcTokenValidator:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_fetcher: JwksFetcher,
        allowed_algorithms: tuple[str, ...] = DEFAULT_ALLOWED_ALGORITHMS,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    ) -> None:
        for algorithm in allowed_algorithms:
            if algorithm.lower() == "none" or algorithm.upper().startswith("HS"):
                raise ValueError(f"symmetric/none algorithm {algorithm!r} is not permitted")
        self._issuer = issuer
        self._audience = audience
        self._jwks_fetcher = jwks_fetcher
        self._allowed_algorithms = allowed_algorithms
        self._leeway = leeway_seconds
        self._keys: dict[str, PyJWK] = {}

    async def _refresh_keys(self) -> None:
        jwks = await self._jwks_fetcher()
        keys: dict[str, PyJWK] = {}
        for key_data in jwks.get("keys", []):
            try:
                parsed = PyJWK(key_data)
            except jwt.PyJWKError:
                logger.warning("skipping unparsable JWKS key")
                continue
            kid = key_data.get("kid")
            if kid:
                keys[kid] = parsed
        self._keys = keys

    async def _key_for(self, kid: str) -> PyJWK:
        if kid not in self._keys:
            await self._refresh_keys()  # rotation: refresh once for new kid
        try:
            return self._keys[kid]
        except KeyError:
            raise AuthenticationError("Token signed with an unknown key.") from None

    async def validate(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise AuthenticationError("Malformed token.") from None

        algorithm = header.get("alg")
        if algorithm not in self._allowed_algorithms:
            raise AuthenticationError("Token algorithm is not permitted.")
        kid = header.get("kid")
        if not kid:
            raise AuthenticationError("Token has no key identifier.")

        key = await self._key_for(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self._allowed_algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("Token has expired.") from None
        except jwt.InvalidAudienceError:
            raise AuthenticationError("Token audience is not accepted.") from None
        except jwt.InvalidIssuerError:
            raise AuthenticationError("Token issuer is not trusted.") from None
        except jwt.InvalidTokenError:
            raise AuthenticationError("Token validation failed.") from None

        return principal_from_oidc_claims(claims)
