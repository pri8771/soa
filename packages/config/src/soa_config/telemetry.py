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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Status, StatusCode

from soa_config.logging import get_correlation_id

logger = logging.getLogger(__name__)

AttributeValue = str | bool | int | float
Attributes = Mapping[str, AttributeValue]


class SpanHandle:
    """Failure-isolated access to the current SDK span.

    HTTP middleware uses this after routing to replace dynamic URLs with a
    bounded route template. No-op telemetry returns the same safe handle.
    """

    def __init__(self, span: otel_trace.Span | None) -> None:
        self._span = span

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            logger.warning("telemetry span attribute update failed", exc_info=True)

    def update_name(self, name: str) -> None:
        if self._span is None:
            return
        try:
            self._span.update_name(name)
        except Exception:
            logger.warning("telemetry span name update failed", exc_info=True)


class Telemetry:
    """Safe facade over a tracer and meter. Use ``Telemetry.noop()`` when
    telemetry is disabled — every call becomes a cheap no-op."""

    def __init__(
        self,
        tracer: otel_trace.Tracer | None,
        meter: otel_metrics.Meter | None,
        *,
        shutdown_callbacks: tuple[Callable[[], object], ...] = (),
    ) -> None:
        self._tracer = tracer
        self._meter = meter
        self._counters: dict[str, otel_metrics.Counter] = {}
        self._gauges: dict[str, otel_metrics._Gauge] = {}
        self._shutdown_callbacks = shutdown_callbacks
        self._shutdown = False

    @classmethod
    def noop(cls) -> "Telemetry":
        return cls(tracer=None, meter=None)

    @contextmanager
    def span(self, name: str, attributes: Attributes | None = None) -> Iterator[SpanHandle]:
        """Record a span around the enclosed block.

        The block always executes; failures inside telemetry itself are
        logged and swallowed. Exceptions raised by the block propagate.
        """
        if self._tracer is None:
            yield SpanHandle(None)
            return
        span_cm = None
        span = None
        try:
            span_cm = self._tracer.start_as_current_span(
                name,
                record_exception=False,
                set_status_on_exception=False,
            )
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
            yield SpanHandle(span)
        except Exception as exc:
            if span_cm is not None:
                try:
                    if span is not None:
                        span.set_attribute("error.type", type(exc).__name__)
                        span.set_status(Status(StatusCode.ERROR))
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

    def shutdown(self) -> None:
        """Flush and close owned SDK providers exactly once.

        Exporter failure is operationally visible but never allowed to
        prevent database or worker shutdown.
        """
        if self._shutdown:
            return
        self._shutdown = True
        for callback in self._shutdown_callbacks:
            try:
                callback()
            except Exception:
                logger.warning("telemetry shutdown failed", exc_info=True)


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
        # Network export must never sit on the request/job critical path.
        # Keep injected/console exporters synchronous for deterministic tests
        # and local debugging; production OTLP uses the SDK's bounded batch
        # queue and flushes it during the owned shutdown lifecycle.
        processor = (
            BatchSpanProcessor(exporter)
            if profile == "otlp" and span_exporter is None
            else SimpleSpanProcessor(exporter)
        )
        tracer_provider.add_span_processor(processor)
    tracer = tracer_provider.get_tracer(service_name)

    # Metrics must flow in real profiles too, not only when tests inject a
    # reader — otherwise every queue metric silently no-ops in deployments.
    reader: MetricReader | None = metric_reader
    if reader is None:
        if profile == "console":
            from opentelemetry.sdk.metrics.export import (
                ConsoleMetricExporter,
                PeriodicExportingMetricReader,
            )

            reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
        elif profile == "otlp":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            assert otlp_endpoint is not None  # validated above for traces
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics")
            )

    meter: otel_metrics.Meter | None = None
    if reader is not None:
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = meter_provider.get_meter(service_name)

    shutdown_callbacks: list[Callable[[], object]] = [tracer_provider.shutdown]
    if meter is not None:
        shutdown_callbacks.append(meter_provider.shutdown)
    return Telemetry(
        tracer=tracer,
        meter=meter,
        shutdown_callbacks=tuple(shutdown_callbacks),
    )
