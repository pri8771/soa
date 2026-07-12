"""API settings — extends the shared service settings base.

Values load from ``SOA_API_``-prefixed environment variables. Production
safety rules (no debug, no dev secrets/credentials) live in ``soa_config``.
"""

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from soa_config import Environment, WebServiceSettings

__all__ = ["ApiSettings", "Environment", "load_settings"]


class ApiSettings(WebServiceSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_API_", frozen=True)

    service_name: str = "soa-api"
    port: int = Field(default=8000, ge=1, le=65535)


def load_settings() -> ApiSettings:
    """Load and validate settings from the environment."""
    return ApiSettings()
