"""API settings — extends the shared service settings base.

Values load from ``SOA_API_``-prefixed environment variables. Production
safety rules (no debug, no dev secrets/credentials) live in ``soa_config``;
API-specific rules (auth configuration) are enforced here.
"""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from soa_config import Environment, WebServiceSettings

__all__ = ["ApiSettings", "Environment", "load_settings"]


class ApiSettings(WebServiceSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_API_", frozen=True)

    service_name: str = "soa-api"
    port: int = Field(default=8000, ge=1, le=65535)

    # Authentication (ADR-010): development identity is local-only; OIDC is
    # the production boundary and must be fully configured there.
    auth_dev_mode: bool = True
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None

    @model_validator(mode="after")
    def _validate_auth_configuration(self) -> Self:
        if self.is_production:
            problems: list[str] = []
            if self.auth_dev_mode:
                problems.append("auth_dev_mode must be disabled in production")
            if not (self.oidc_issuer and self.oidc_audience and self.oidc_jwks_url):
                problems.append("production requires oidc_issuer, oidc_audience, and oidc_jwks_url")
            if problems:
                raise ValueError("; ".join(problems))
        return self


def load_settings() -> ApiSettings:
    """Load and validate settings from the environment."""
    return ApiSettings()
