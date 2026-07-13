from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment


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
        )
    return ApiSettings(environment=environment)


def app_with_routes(environment: Environment) -> FastAPI:
    app = create_app(make_settings(environment))

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
    client = TestClient(app_with_routes(Environment.PRODUCTION), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text
    assert "RuntimeError" not in response.text
    assert body["error"]["correlation_id"]


def test_development_errors_include_exception_summary() -> None:
    client = TestClient(app_with_routes(Environment.DEVELOPMENT), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    assert "RuntimeError" in response.json()["error"]["message"]


def test_production_disables_docs() -> None:
    client = TestClient(app_with_routes(Environment.PRODUCTION), raise_server_exceptions=False)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
