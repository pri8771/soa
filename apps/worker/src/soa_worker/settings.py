"""Typed worker settings.

Settings load from environment variables prefixed with ``SOA_WORKER_`` and
fail fast on invalid combinations.
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOA_WORKER_", frozen=True)

    service_name: str = "soa-worker"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @model_validator(mode="after")
    def _forbid_debug_in_production(self) -> Self:
        if self.is_production and self.debug:
            raise ValueError("debug must not be enabled in production")
        return self


def load_settings() -> WorkerSettings:
    """Load and validate settings from the environment."""
    return WorkerSettings()
