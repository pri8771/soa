import asyncio

from soa_config.logging import get_correlation_id
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import Environment, WorkerSettings
from soa_worker.worker import Worker

FAST = WorkerSettings(
    environment=Environment.TEST,
    poll_interval_seconds=0.01,
    heartbeat_interval_seconds=0.01,
)


async def test_job_correlation_id_is_bound_during_handler() -> None:
    registry = HandlerRegistry()
    seen: list[str | None] = []

    @registry.register("traced.job")
    async def traced(job: JobEnvelope) -> None:
        seen.append(get_correlation_id())

    queue = [JobEnvelope(job_type="traced.job", correlation_id="job-corr-7")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(FAST, registry, fetch_job=fetch)
    task = asyncio.create_task(worker.run())
    while worker.jobs_completed == 0:
        await asyncio.sleep(0.005)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)

    assert seen == ["job-corr-7"]
    assert get_correlation_id() is None


async def test_job_without_correlation_id_gets_generated_one() -> None:
    registry = HandlerRegistry()
    seen: list[str | None] = []

    @registry.register("traced.job")
    async def traced(job: JobEnvelope) -> None:
        seen.append(get_correlation_id())

    queue = [JobEnvelope(job_type="traced.job")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(FAST, registry, fetch_job=fetch)
    task = asyncio.create_task(worker.run())
    while worker.jobs_completed == 0:
        await asyncio.sleep(0.005)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)

    assert len(seen) == 1
    assert seen[0], "a correlation ID should be generated when none is supplied"
