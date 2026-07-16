from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_config.telemetry import configure_telemetry
from soa_db import Base, DatabaseSessions, create_database_engine


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/tracing.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield DatabaseSessions(engine)
    finally:
        await engine.dispose()


def test_api_request_creates_correlated_span(database: DatabaseSessions) -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(
        service_name="soa-api",
        environment="test",
        profile="none",
        span_exporter=exporter,
    )
    app = create_app(ApiSettings(environment=Environment.TEST), telemetry=telemetry, db=database)
    client = TestClient(app)

    response = client.get("/health/live", headers={"X-Request-ID": "span-corr-1"})
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "HTTP GET /health/live"
    assert span.attributes is not None
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.route"] == "/health/live"
    assert span.attributes["soa.correlation_id"] == "span-corr-1"


def test_dynamic_url_values_never_become_trace_names_or_attributes(
    database: DatabaseSessions,
) -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(
        service_name="soa-api",
        environment="test",
        profile="none",
        span_exporter=exporter,
    )
    app = create_app(ApiSettings(environment=Environment.TEST), telemetry=telemetry, db=database)
    client = TestClient(app)

    response = client.get(
        "/orgs/private-customer-slug",
        headers={"X-Dev-User": "user:admin"},
    )
    assert response.status_code == 404
    (span,) = exporter.get_finished_spans()
    assert span.name == "HTTP GET /orgs/{organization_slug}"
    assert "private-customer-slug" not in span.to_json()
