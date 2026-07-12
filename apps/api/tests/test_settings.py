import pytest
from pydantic import ValidationError

from soa_api.settings import ApiSettings, Environment


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
