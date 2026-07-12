import asyncio

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from soa_config.telemetry import configure_telemetry
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import Environment, WorkerSettings
from soa_worker.worker import Worker

FAST = WorkerSettings(
    environment=Environment.TEST,
    poll_interval_seconds=0.01,
    heartbeat_interval_seconds=0.01,
)


async def test_job_handler_creates_correlated_span() -> None:
    exporter = InMemorySpanExporter()
    telemetry = configure_telemetry(
        service_name="soa-worker",
        environment="test",
        profile="none",
        span_exporter=exporter,
    )
    registry = HandlerRegistry()

    @registry.register("traced.job")
    async def traced(job: JobEnvelope) -> None:
        pass

    queue = [JobEnvelope(job_type="traced.job", correlation_id="worker-span-1")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(FAST, registry, fetch_job=fetch, telemetry=telemetry)
    task = asyncio.create_task(worker.run())
    while worker.jobs_completed == 0:
        await asyncio.sleep(0.005)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "job traced.job"
    assert span.attributes is not None
    assert span.attributes["soa.job_type"] == "traced.job"
    assert span.attributes["soa.correlation_id"] == "worker-span-1"
