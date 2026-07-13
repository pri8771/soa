"""Base service settings shared by the API and worker.

Rules enforced here (per docs/SECURITY_OPERATIONS.md and FND-007):

- Production startup fails when a required production setting is missing or
  still carries a development default.
- Development-only flags (``debug``) fail startup in production.
- Secret values are ``SecretStr`` — they render masked in ``repr``/``str``
  and serialize masked in ``safe_dump()``. Nothing in this module ever
  returns a plaintext secret except an explicit ``get_secret_value()`` call
  at the point of use.
"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development defaults, deliberately recognizable. Production refuses them.
DEV_SECRET_KEY = "dev-insecure-secret-key-do-not-use-in-production"
_DEV_DB_CREDENTIALS = ("soa_dev", "soa_dev_password")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    service_name: str = "soa-service"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    secret_key: SecretStr = SecretStr(DEV_SECRET_KEY)
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://soa_dev:soa_dev_password@localhost:5432/soa"
    )
    telemetry_profile: Literal["none", "console", "otlp"] = "none"
    otlp_endpoint: str | None = None

    # Secret store (SEC-005): where tenant-provided secret VALUES live.
    # The database stores only ``secretref://`` references. memory/file
    # are development backends; production must use a real manager.
    secrets_backend: Literal["memory", "file", "aws-secrets-manager"] = "memory"
    secrets_directory: str | None = None  # required by the file backend
    secrets_aws_region: str | None = None  # required by the AWS backend

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def is_development_like(self) -> bool:
        return self.environment in (Environment.DEVELOPMENT, Environment.TEST)

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Self:
        if not self.is_production:
            return self
        problems: list[str] = []
        if self.debug:
            problems.append("debug must not be enabled in production")
        if self.secret_key.get_secret_value() == DEV_SECRET_KEY:
            problems.append("secret_key must be set explicitly in production")
        elif len(self.secret_key.get_secret_value()) < 32:
            problems.append("secret_key must be at least 32 characters in production")
        raw_db = self.database_url.get_secret_value()
        if any(credential in raw_db for credential in _DEV_DB_CREDENTIALS):
            problems.append("database_url must not use development credentials in production")
        if self.secrets_backend != "aws-secrets-manager":
            problems.append(
                "production requires the aws-secrets-manager secrets backend — "
                "memory/file stores are development-only"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_secrets_backend(self) -> Self:
        if self.secrets_backend == "file" and not self.secrets_directory:
            raise ValueError("secrets_backend 'file' requires secrets_directory")
        if self.secrets_backend == "aws-secrets-manager" and not self.secrets_aws_region:
            raise ValueError("secrets_backend 'aws-secrets-manager' requires secrets_aws_region")
        return self

    @model_validator(mode="after")
    def _validate_telemetry(self) -> Self:
        if self.telemetry_profile == "otlp" and not self.otlp_endpoint:
            raise ValueError("telemetry_profile 'otlp' requires otlp_endpoint")
        return self

    def safe_dump(self) -> dict[str, Any]:
        """Settings as a JSON-safe dict with every secret masked.

        Safe to log at startup. Secrets serialize as ``**********``.
        """
        return self.model_dump(mode="json")


class WebServiceSettings(BaseServiceSettings):
    """Base for HTTP-serving processes."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
