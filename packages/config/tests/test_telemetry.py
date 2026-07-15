from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from soa_config.logging import correlation_context
from soa_config.telemetry import Telemetry, configure_telemetry


def make_telemetry(exporter: SpanExporter) -> Telemetry:
    return configure_telemetry(
        service_name="soa-test",
        environment="test",
        profile="none",
        span_exporter=exporter,
    )


def test_noop_profile_records_nothing_and_costs_nothing() -> None:
    telemetry = configure_telemetry(service_name="s", environment="test", profile="none")
    with telemetry.span("anything", attributes={"a": 1}):
        pass
    telemetry.add_to_counter("count", 1)  # must not raise


def test_span_is_exported_with_attributes_and_correlation() -> None:
    exporter = InMemorySpanExporter()
    telemetry = make_telemetry(exporter)
    with correlation_context("corr-tel-1"):
        with telemetry.span("unit-of-work", attributes={"soa.job_type": "demo"}):
            pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "unit-of-work"
    assert span.attributes is not None
    assert span.attributes["soa.job_type"] == "demo"
    assert span.attributes["soa.correlation_id"] == "corr-tel-1"


def test_business_exception_propagates_with_safe_type_only() -> None:
    exporter = InMemorySpanExporter()
    telemetry = make_telemetry(exporter)
    try:
        with telemetry.span("failing-work"):
            raise ValueError("CANARY customer document and credential")
    except ValueError:
        pass
    else:
        raise AssertionError("business exception must propagate")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code.name == "ERROR"
    assert span.attributes is not None
    assert span.attributes["error.type"] == "ValueError"
    assert span.events == (), "untrusted exception messages and stacks must not be exported"
    assert "CANARY" not in span.to_json()


class ExplodingExporter(SpanExporter):
    def export(self, spans: object) -> SpanExportResult:
        raise RuntimeError("exporter is down")

    def shutdown(self) -> None:
        pass


def test_broken_exporter_never_breaks_business_code() -> None:
    telemetry = make_telemetry(ExplodingExporter())
    result: list[int] = []
    with telemetry.span("resilient-work"):
        result.append(42)
    assert result == [42]


def test_otlp_profile_requires_endpoint() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires otlp_endpoint"):
        configure_telemetry(service_name="s", environment="test", profile="otlp")


def test_counter_records_via_metric_reader() -> None:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    telemetry = configure_telemetry(
        service_name="soa-test",
        environment="test",
        profile="none",
        metric_reader=reader,
    )
    telemetry.add_to_counter("soa.test.events", 3, attributes={"kind": "demo"})
    data = reader.get_metrics_data()
    assert data is not None
    points = [
        point
        for resource_metric in data.resource_metrics
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
        for point in metric.data.data_points
    ]
    assert points and points[0].value == 3


def test_shutdown_is_idempotent_and_exporter_failure_does_not_block_cleanup() -> None:
    calls: list[str] = []

    def broken() -> None:
        calls.append("broken")
        raise RuntimeError("collector unavailable")

    def healthy() -> None:
        calls.append("healthy")

    telemetry = Telemetry(
        tracer=None,
        meter=None,
        shutdown_callbacks=(broken, healthy),
    )
    telemetry.shutdown()
    telemetry.shutdown()
    assert calls == ["broken", "healthy"]
