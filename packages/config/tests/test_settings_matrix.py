"""Configuration matrix tests for shared service settings."""

import pytest
from pydantic import SecretStr, ValidationError

from soa_config import DEV_SECRET_KEY, BaseServiceSettings, Environment

PROD_SECRET = "x" * 40
PROD_DB = "postgresql+asyncpg://svc:strong-managed-password@db.internal:5432/soa"


def test_development_defaults_are_valid() -> None:
    settings = BaseServiceSettings()
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.is_development_like


def test_production_with_explicit_values_is_valid() -> None:
    settings = BaseServiceSettings(
        environment=Environment.PRODUCTION,
        secret_key=SecretStr(PROD_SECRET),
        database_url=SecretStr(PROD_DB),
        secrets_backend="aws-secrets-manager",
        secrets_aws_region="eu-central-1",
    )
    assert settings.is_production


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"debug": True}, "debug must not be enabled in production"),
        (
            {"secret_key": SecretStr(DEV_SECRET_KEY)},
            "secret_key must be set explicitly in production",
        ),
        (
            {"secret_key": SecretStr("short")},
            "secret_key must be at least 32 characters",
        ),
        (
            {
                "database_url": SecretStr(
                    "postgresql+asyncpg://soa_dev:soa_dev_password@prod-db:5432/soa"
                )
            },
            "development credentials",
        ),
        (
            {"secrets_backend": "memory", "secrets_aws_region": None},
            "requires the aws-secrets-manager secrets backend",
        ),
    ],
)
def test_production_rejects_unsafe_configuration(
    overrides: dict[str, object], expected_message: str
) -> None:
    base: dict[str, object] = {
        "environment": Environment.PRODUCTION,
        "secret_key": SecretStr(PROD_SECRET),
        "database_url": SecretStr(PROD_DB),
        "secrets_backend": "aws-secrets-manager",
        "secrets_aws_region": "eu-central-1",
    }
    base.update(overrides)
    with pytest.raises(ValidationError, match=expected_message):
        BaseServiceSettings(**base)  # type: ignore[arg-type]


def test_development_tolerates_dev_defaults() -> None:
    settings = BaseServiceSettings(environment=Environment.DEVELOPMENT, debug=True)
    assert settings.debug is True


def test_secrets_are_masked_in_repr_str_and_safe_dump() -> None:
    settings = BaseServiceSettings(
        secret_key=SecretStr("super-sensitive-value-abcdefghijklmn"),
        database_url=SecretStr("postgresql+asyncpg://user:sensitive-pw@host:5432/db"),
    )
    for rendered in (repr(settings), str(settings), str(settings.safe_dump())):
        assert "super-sensitive-value" not in rendered
        assert "sensitive-pw" not in rendered
    dumped = settings.safe_dump()
    assert dumped["secret_key"] == "**********"
    assert dumped["database_url"] == "**********"


def test_secret_is_retrievable_only_explicitly() -> None:
    settings = BaseServiceSettings(secret_key=SecretStr("explicit-retrieval-test-0123456789"))
    assert settings.secret_key.get_secret_value() == "explicit-retrieval-test-0123456789"
