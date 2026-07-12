from fastapi.testclient import TestClient

import soa_api
from soa_api.app import create_app
from soa_api.dependencies import Dependencies
from soa_api.settings import ApiSettings, Environment


def make_client(settings: ApiSettings | None = None) -> TestClient:
    app = create_app(settings or ApiSettings(environment=Environment.TEST))
    return TestClient(app, raise_server_exceptions=False)


def test_liveness() -> None:
    client = make_client()
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_with_no_dependencies_is_ok() -> None:
    client = make_client()
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"] == []


def test_readiness_reports_unhealthy_dependency_as_503() -> None:
    app = create_app(ApiSettings(environment=Environment.TEST))
    deps: Dependencies = app.state.dependencies

    async def failing_check() -> bool:
        raise RuntimeError("connection refused")

    async def passing_check() -> bool:
        return True

    deps.register_readiness_check("database", failing_check)
    deps.register_readiness_check("object-store", passing_check)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    statuses = {dep["name"]: dep["healthy"] for dep in body["dependencies"]}
    assert statuses == {"database": False, "object-store": True}


def test_version_endpoint() -> None:
    client = make_client()
    response = client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "soa-api"
    assert body["version"] == soa_api.__version__
    assert body["environment"] == "test"


def test_correlation_id_is_echoed() -> None:
    client = make_client()
    response = client.get("/health/live", headers={"X-Request-ID": "corr-123"})
    assert response.headers["X-Request-ID"] == "corr-123"


def test_correlation_id_is_generated_when_absent() -> None:
    client = make_client()
    response = client.get("/health/live")
    assert response.headers["X-Request-ID"]
