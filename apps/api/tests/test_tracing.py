from pathlib import Path

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_config.telemetry import configure_telemetry
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_storage import MemoryObjectStore


def test_api_request_creates_correlated_span() -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(
        service_name="soa-api",
        environment="test",
        profile="none",
        span_exporter=exporter,
    )
    app = create_app(ApiSettings(environment=Environment.TEST), telemetry=telemetry)
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


async def test_dynamic_url_values_never_become_trace_names_or_attributes(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(
        service_name="soa-api",
        environment="test",
        profile="none",
        span_exporter=exporter,
    )
    # The /orgs/{slug} route queries the database (404 for an unknown org),
    # so give the app a real in-memory test DB — otherwise it connects to
    # the default Postgres, which the unit-test CI job has no service for.
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/tracing.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
        telemetry=telemetry,
    )
    client = TestClient(app)

    response = client.get(
        "/orgs/private-customer-slug",
        headers={"X-Dev-User": "user:admin"},
    )
    assert response.status_code == 404
    (span,) = exporter.get_finished_spans()
    assert span.name == "HTTP GET /orgs/{organization_slug}"
    assert "private-customer-slug" not in span.to_json()
