import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from soa_api.app import create_app
from soa_api.auth.dependency import CurrentPrincipal
from soa_api.settings import ApiSettings, Environment


def app_with_me_route(settings: ApiSettings) -> FastAPI:
    app = create_app(settings)

    @app.get("/whoami")
    async def whoami(principal: CurrentPrincipal) -> dict[str, str | None]:
        return {
            "subject": principal.subject,
            "issuer": principal.issuer,
            "email": principal.email,
            "method": principal.auth_method.value,
        }

    return app


def test_dev_identity_resolves_seeded_user_by_email() -> None:
    app = app_with_me_route(ApiSettings(environment=Environment.TEST))
    client = TestClient(app)
    response = client.get("/whoami", headers={"X-Dev-User": "reviewer@northstar.example"})
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "reviewer@northstar.example"
    assert body["issuer"] == "soa-dev"
    assert body["method"] == "dev"


def test_dev_identity_resolves_seeded_user_by_alias() -> None:
    app = app_with_me_route(ApiSettings(environment=Environment.TEST))
    client = TestClient(app)
    response = client.get("/whoami", headers={"X-Dev-User": "user:supervisor"})
    assert response.status_code == 200
    assert response.json()["email"] == "supervisor@northstar.example"


def test_unknown_dev_user_is_unauthorized() -> None:
    app = app_with_me_route(ApiSettings(environment=Environment.TEST))
    client = TestClient(app)
    response = client.get("/whoami", headers={"X-Dev-User": "intruder@evil.example"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_missing_credentials_are_unauthorized() -> None:
    app = app_with_me_route(ApiSettings(environment=Environment.TEST))
    client = TestClient(app)
    assert client.get("/whoami").status_code == 401


def test_dev_mode_flag_is_ignored_outside_dev_like_environments() -> None:
    settings = ApiSettings(
        environment=Environment.STAGING,
        auth_dev_mode=True,
        secret_key=SecretStr("staging-secret-key-0123456789abcdef"),
        database_url=SecretStr("postgresql+asyncpg://svc:pw@db:5432/soa"),
    )
    app = app_with_me_route(settings)
    client = TestClient(app)
    response = client.get("/whoami", headers={"X-Dev-User": "reviewer@northstar.example"})
    assert response.status_code == 401, "dev header must be inert outside development/test"


def test_production_startup_fails_with_dev_mode_enabled() -> None:
    with pytest.raises(ValidationError, match="auth_dev_mode must be disabled"):
        ApiSettings(
            environment=Environment.PRODUCTION,
            auth_dev_mode=True,
            secret_key=SecretStr("prod-secret-key-0123456789abcdef00"),
            database_url=SecretStr("postgresql+asyncpg://svc:pw@db:5432/soa"),
            secrets_backend="aws-secrets-manager",
            secrets_aws_region="eu-central-1",
            oidc_issuer="https://id.example.com/",
            oidc_audience="soa-api",
            oidc_jwks_url="https://id.example.com/jwks",
        )


def test_production_startup_fails_without_oidc_configuration() -> None:
    with pytest.raises(ValidationError, match="requires oidc_issuer"):
        ApiSettings(
            environment=Environment.PRODUCTION,
            auth_dev_mode=False,
            secret_key=SecretStr("prod-secret-key-0123456789abcdef00"),
            database_url=SecretStr("postgresql+asyncpg://svc:pw@db:5432/soa"),
            secrets_backend="aws-secrets-manager",
            secrets_aws_region="eu-central-1",
        )
