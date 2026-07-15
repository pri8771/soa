import io
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_config.logging import JsonFormatter
from soa_config.telemetry import Telemetry


class _Payload(BaseModel):
    quantity: int


def make_settings(environment: Environment) -> ApiSettings:
    if environment is Environment.PRODUCTION:
        return ApiSettings(
            environment=environment,
            secret_key=SecretStr("test-production-secret-key-0123456789"),
            database_url=SecretStr("postgresql+asyncpg://svc:managed-pw@db.internal:5432/soa"),
            auth_dev_mode=False,
            oidc_issuer="https://id.example.com/",
            oidc_audience="soa-api",
            oidc_jwks_url="https://id.example.com/.well-known/jwks.json",
            storage_endpoint_url="https://s3.internal.example.com",
            storage_access_key="svc-storage",
            storage_secret_key="managed-storage-secret",
            clamav_host="clamav.internal",
            secrets_backend="aws-secrets-manager",
            secrets_aws_region="eu-central-1",
            telemetry_profile="otlp",
            otlp_endpoint="https://telemetry.internal.example",
            email_intake_secret=SecretStr("e" * 40),
            outbound_destination_allowlist=("erp.example.com",),
        )
    return ApiSettings(environment=environment)


def app_with_routes(environment: Environment) -> FastAPI:
    # Exercise production settings without starting a real OTLP exporter from
    # this isolated error-envelope test.
    app = create_app(make_settings(environment), telemetry=Telemetry.noop())

    @app.post("/echo")
    async def echo(payload: _Payload) -> _Payload:
        return payload

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


def test_not_found_uses_error_envelope() -> None:
    client = TestClient(app_with_routes(Environment.TEST), raise_server_exceptions=False)
    response = client.get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["correlation_id"]


def test_validation_error_lists_field_details() -> None:
    client = TestClient(app_with_routes(Environment.TEST), raise_server_exceptions=False)
    response = client.post("/echo", json={"quantity": "not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    locations = [detail["location"] for detail in body["error"]["details"]]
    assert ["body", "quantity"] in locations


def test_production_errors_hide_internal_detail() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(service_name="soa-api", environment="production"))
    error_logger = logging.getLogger("soa_api.errors")
    prior_handlers = error_logger.handlers
    prior_propagate = error_logger.propagate
    prior_level = error_logger.level
    error_logger.handlers = [handler]
    error_logger.propagate = False
    error_logger.setLevel(logging.ERROR)
    try:
        client = TestClient(app_with_routes(Environment.PRODUCTION), raise_server_exceptions=False)
        response = client.get("/boom", headers={"X-Request-ID": "error-corr-1"})
    finally:
        error_logger.handlers = prior_handlers
        error_logger.propagate = prior_propagate
        error_logger.setLevel(prior_level)

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text
    assert "RuntimeError" not in response.text
    assert body["error"]["correlation_id"] == "error-corr-1"
    rendered_log = stream.getvalue()
    assert "secret internal detail" not in rendered_log
    logged = json.loads(rendered_log)
    assert logged["correlation_id"] == "error-corr-1"
    assert logged["exception"] == {"type": "RuntimeError"}
    assert logged["http_method"] == "GET"
    assert logged["http_route"] == "/boom"


def test_development_errors_include_exception_summary() -> None:
    client = TestClient(app_with_routes(Environment.DEVELOPMENT), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    assert "RuntimeError" in response.json()["error"]["message"]


def test_production_disables_docs() -> None:
    client = TestClient(app_with_routes(Environment.PRODUCTION), raise_server_exceptions=False)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
