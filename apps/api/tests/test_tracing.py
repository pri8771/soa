from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_config.telemetry import configure_telemetry


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
    assert span.attributes["soa.correlation_id"] == "span-corr-1"
