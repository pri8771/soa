import asyncio
import time

from fastapi.testclient import TestClient

import soa_api
from soa_api.app import create_app
from soa_api.dependencies import Dependencies
from soa_api.settings import ApiSettings, Environment
from soa_db import DatabaseSessions, create_database_engine


def make_db() -> DatabaseSessions:
    return DatabaseSessions(create_database_engine("sqlite+aiosqlite:///:memory:"))


def make_client(settings: ApiSettings | None = None) -> TestClient:
    app = create_app(settings or ApiSettings(environment=Environment.TEST), db=make_db())
    return TestClient(app, raise_server_exceptions=False)


def test_liveness() -> None:
    client = make_client()
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_with_healthy_database_is_ok() -> None:
    client = make_client()
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"] == [
        {"name": "database", "healthy": True},
        {"name": "object-storage", "healthy": True},
        {"name": "malware-scanner", "healthy": True},
    ]


def test_readiness_reports_unhealthy_dependency_as_503() -> None:
    app = create_app(ApiSettings(environment=Environment.TEST), db=make_db())
    deps: Dependencies = app.state.dependencies

    async def failing_check() -> bool:
        raise RuntimeError("connection refused")

    deps.register_readiness_check("forced-failure", failing_check)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    statuses = {dep["name"]: dep["healthy"] for dep in body["dependencies"]}
    assert statuses == {
        "database": True,
        "object-storage": True,
        "malware-scanner": True,
        "forced-failure": False,
    }


async def test_readiness_checks_run_concurrently() -> None:
    dependencies = Dependencies(settings=ApiSettings(environment=Environment.TEST))

    async def slow_check() -> bool:
        await asyncio.sleep(0.05)
        return True

    dependencies.register_readiness_check("first", slow_check)
    dependencies.register_readiness_check("second", slow_check)
    started = time.perf_counter()

    results = await dependencies.run_readiness_checks()

    assert [result.name for result in results] == ["first", "second"]
    assert time.perf_counter() - started < 0.09


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
