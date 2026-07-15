"""Production worker settings fail closed around event delivery."""

import pytest
from pydantic import SecretStr, ValidationError

from soa_worker.settings import Environment, WorkerSettings


def production_settings(**overrides: object) -> dict[str, object]:
    return {
        "environment": Environment.PRODUCTION,
        "secret_key": SecretStr("x" * 40),
        "database_url": SecretStr("postgresql://worker:real@db.internal/soa"),
        "secrets_backend": "gcp-secret-manager",
        "secrets_gcp_project": "soa-prod",
        "storage_backend": "gcs",
        "storage_gcs_project": "soa-prod",
        "export_destination_allowlist": ("erp.example",),
        "outbox_publish_url": "https://events.example/domain-events",
        "telemetry_profile": "otlp",
        "otlp_endpoint": "https://telemetry.internal.example",
        **overrides,
    }


def test_production_requires_strong_outbox_authentication() -> None:
    with pytest.raises(ValidationError, match="outbox_signing_secret"):
        WorkerSettings(**production_settings())
    with pytest.raises(ValidationError, match="outbox_signing_secret"):
        WorkerSettings(**production_settings(outbox_signing_secret=SecretStr("short")))


def test_production_accepts_authenticated_https_outbox() -> None:
    settings = WorkerSettings(**production_settings(outbox_signing_secret=SecretStr("s" * 32)))
    assert settings.is_production


def test_hosted_rate_cards_are_explicit_and_overrideable() -> None:
    settings = WorkerSettings(
        environment=Environment.TEST,
        anthropic_pricing_reference="contract:v9",
        anthropic_input_cents_per_million=321,
        anthropic_output_cents_per_million=1_234,
    )
    assert settings.anthropic_pricing_reference == "contract:v9"
    assert settings.anthropic_input_cents_per_million == 321
    assert settings.anthropic_output_cents_per_million == 1_234


def test_hosted_rate_cards_reject_negative_rates_and_blank_references() -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            environment=Environment.TEST,
            gemini_input_cents_per_million=-1,
        )
    with pytest.raises(ValidationError):
        WorkerSettings(
            environment=Environment.TEST,
            hosted_openai_pricing_reference="",
        )


def test_destination_allowlist_requires_exact_hostnames() -> None:
    with pytest.raises(ValidationError, match="exact hostnames"):
        WorkerSettings(
            environment=Environment.TEST,
            export_destination_allowlist=("*.example.com",),
        )
