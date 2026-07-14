import pytest
from pydantic import SecretStr, ValidationError

from soa_api.settings import ApiSettings, Environment

# The interdependent fields production validation requires, so a test can
# vary ONE axis (here: the storage backend) and keep the rest valid.
_PROD_BASE = dict(
    environment=Environment.PRODUCTION,
    auth_dev_mode=False,
    secret_key=SecretStr("x" * 40),
    database_url=SecretStr("postgresql://produser:realsecret@db.internal/soa"),
    secrets_backend="gcp-secret-manager",
    secrets_gcp_project="soa-pilot",
    oidc_issuer="https://issuer.example",
    oidc_audience="soa-api",
    oidc_jwks_url="https://issuer.example/jwks",
    clamav_host="clamav.internal",
)


def test_defaults_are_development() -> None:
    settings = ApiSettings()
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.debug is False
    assert settings.is_production is False


def test_debug_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="debug must not be enabled in production"):
        ApiSettings(environment=Environment.PRODUCTION, debug=True)


def test_environment_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOA_API_ENVIRONMENT", "staging")
    settings = ApiSettings()
    assert settings.environment is Environment.STAGING


def test_settings_are_frozen() -> None:
    settings = ApiSettings()
    with pytest.raises(ValidationError):
        settings.debug = True  # type: ignore[misc]


def test_default_storage_backend_is_s3() -> None:
    assert ApiSettings().storage_backend == "s3"


def test_production_gcs_storage_needs_a_project() -> None:
    with pytest.raises(ValidationError, match="storage_gcs_project"):
        ApiSettings(**_PROD_BASE, storage_backend="gcs")


def test_production_gcs_storage_does_not_require_s3_endpoint() -> None:
    # With the GCS backend the S3 endpoint/keys are irrelevant and must
    # NOT be demanded.
    settings = ApiSettings(**_PROD_BASE, storage_backend="gcs", storage_gcs_project="soa-pilot")
    assert settings.is_production
    assert settings.storage_endpoint_url is None


def test_production_s3_storage_still_requires_its_endpoint() -> None:
    with pytest.raises(ValidationError, match="storage_endpoint_url"):
        ApiSettings(**_PROD_BASE, storage_backend="s3")
