"""Traces and metrics facade with no-op, console, and OTLP profiles.

Application code never touches the OpenTelemetry SDK directly — it calls the
``Telemetry`` facade. Two guarantees:

1. Profiles select the exporter: ``none`` (default, zero overhead),
   ``console`` (local development), ``otlp`` (collector endpoint).
2. Telemetry failure never breaks business processing: every facade call
   swallows telemetry-side exceptions. The wrapped business code's own
   exceptions always propagate normally.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)

from soa_config.logging import get_correlation_id

logger = logging.getLogger(__name__)

AttributeValue = str | bool | int | float
Attributes = Mapping[str, AttributeValue]


class Telemetry:
    """Safe facade over a tracer and meter. Use ``Telemetry.noop()`` when
    telemetry is disabled — every call becomes a cheap no-op."""

    def __init__(
        self,
        tracer: otel_trace.Tracer | None,
        meter: otel_metrics.Meter | None,
    ) -> None:
        self._tracer = tracer
        self._meter = meter
        self._counters: dict[str, otel_metrics.Counter] = {}
        self._gauges: dict[str, otel_metrics._Gauge] = {}

    @classmethod
    def noop(cls) -> "Telemetry":
        return cls(tracer=None, meter=None)

    @contextmanager
    def span(self, name: str, attributes: Attributes | None = None) -> Iterator[None]:
        """Record a span around the enclosed block.

        The block always executes; failures inside telemetry itself are
        logged and swallowed. Exceptions raised by the block propagate.
        """
        if self._tracer is None:
            yield
            return
        span_cm = None
        span = None
        try:
            span_cm = self._tracer.start_as_current_span(name, record_exception=True)
            span = span_cm.__enter__()
            merged: dict[str, AttributeValue] = dict(attributes or {})
            correlation_id = get_correlation_id()
            if correlation_id is not None:
                merged["soa.correlation_id"] = correlation_id
            for key, value in merged.items():
                span.set_attribute(key, value)
        except Exception:
            logger.warning("telemetry span start failed", exc_info=True)
            span_cm = None
        try:
            yield
        except Exception as exc:
            if span_cm is not None:
                try:
                    span_cm.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    logger.warning("telemetry span exit failed", exc_info=True)
                span_cm = None
            raise
        finally:
            if span_cm is not None:
                try:
                    span_cm.__exit__(None, None, None)
                except Exception:
                    logger.warning("telemetry span exit failed", exc_info=True)

    def add_to_counter(
        self, name: str, amount: int = 1, attributes: Attributes | None = None
    ) -> None:
        if self._meter is None:
            return
        try:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._meter.create_counter(name)
                self._counters[name] = counter
            counter.add(amount, dict(attributes or {}))
        except Exception:
            logger.warning("telemetry counter update failed", exc_info=True)

    def set_gauge(self, name: str, value: float, attributes: Attributes | None = None) -> None:
        """Record a point-in-time measurement (queue depth, oldest age)."""
        if self._meter is None:
            return
        try:
            gauge = self._gauges.get(name)
            if gauge is None:
                gauge = self._meter.create_gauge(name)
                self._gauges[name] = gauge
            gauge.set(value, dict(attributes or {}))
        except Exception:
            logger.warning("telemetry gauge update failed", exc_info=True)


def configure_telemetry(
    *,
    service_name: str,
    environment: str,
    profile: str = "none",
    otlp_endpoint: str | None = None,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Telemetry:
    """Build a Telemetry facade for the requested profile.

    ``span_exporter``/``metric_reader`` exist for tests (in-memory capture)
    and override the profile's default exporter.
    """
    if profile == "none" and span_exporter is None and metric_reader is None:
        return Telemetry.noop()

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment.name": environment,
        }
    )

    exporter: SpanExporter | None = span_exporter
    if exporter is None:
        if profile == "console":
            exporter = ConsoleSpanExporter()
        elif profile == "otlp":
            if not otlp_endpoint:
                raise ValueError("otlp telemetry profile requires otlp_endpoint")
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")

    tracer_provider = TracerProvider(resource=resource)
    if exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tracer_provider.get_tracer(service_name)

    meter: otel_metrics.Meter | None = None
    if metric_reader is not None:
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        meter = meter_provider.get_meter(service_name)

    return Telemetry(tracer=tracer, meter=meter)
