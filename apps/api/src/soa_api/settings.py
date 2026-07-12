"""Typed application settings.

Settings load from environment variables prefixed with ``SOA_API_``.
Startup fails fast on invalid combinations (for example ``debug=True`` in
production) rather than degrading silently.
"""

from enum import StrEnum
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_API_", frozen=True)

    service_name: str = "soa-api"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @model_validator(mode="after")
    def _forbid_debug_in_production(self) -> Self:
        if self.is_production and self.debug:
            raise ValueError("debug must not be enabled in production")
        return self


def load_settings() -> ApiSettings:
    """Load and validate settings from the environment."""
    return ApiSettings()
