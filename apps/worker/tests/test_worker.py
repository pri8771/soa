import asyncio
import os
import signal

import soa_worker
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import Environment, WorkerSettings
from soa_worker.worker import Worker, WorkerState

FAST = WorkerSettings(
    environment=Environment.TEST,
    poll_interval_seconds=0.01,
    heartbeat_interval_seconds=0.01,
)


async def wait_for(predicate, timeout: float = 2.0):  # type: ignore[no-untyped-def]
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.005)


async def test_startup_metadata_and_clean_shutdown() -> None:
    worker = Worker(FAST, HandlerRegistry())
    metadata = worker.metadata()
    assert metadata["service"] == "soa-worker"
    assert metadata["version"] == soa_worker.__version__
    assert metadata["environment"] == "test"

    task = asyncio.create_task(worker.run())
    await wait_for(lambda: worker.state is WorkerState.RUNNING)
    await wait_for(lambda: worker.last_heartbeat_at is not None)

    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)
    assert worker.state is WorkerState.STOPPED


async def test_active_handler_completes_before_shutdown() -> None:
    registry = HandlerRegistry()
    handler_finished = asyncio.Event()
    handler_started = asyncio.Event()

    @registry.register("slow.job")
    async def slow_job(job: JobEnvelope) -> None:
        handler_started.set()
        await asyncio.sleep(0.1)
        handler_finished.set()

    queue: list[JobEnvelope] = [JobEnvelope(job_type="slow.job")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(FAST, registry, fetch_job=fetch)
    task = asyncio.create_task(worker.run())

    await handler_started.wait()
    worker.request_stop()  # stop requested mid-handler
    await asyncio.wait_for(task, timeout=2)

    assert handler_finished.is_set(), "handler must not be abandoned on shutdown"
    assert worker.jobs_completed == 1
    assert worker.active_job is None
    assert worker.state is WorkerState.STOPPED


async def test_failed_handler_is_recorded_and_loop_continues() -> None:
    registry = HandlerRegistry()

    @registry.register("bad.job")
    async def bad_job(job: JobEnvelope) -> None:
        raise RuntimeError("boom")

    @registry.register("good.job")
    async def good_job(job: JobEnvelope) -> None:
        pass

    queue = [JobEnvelope(job_type="good.job"), JobEnvelope(job_type="bad.job")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(FAST, registry, fetch_job=fetch)
    task = asyncio.create_task(worker.run())

    await wait_for(lambda: worker.jobs_completed == 1 and worker.jobs_failed == 1)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)


async def test_sigterm_triggers_graceful_stop() -> None:
    worker = Worker(FAST, HandlerRegistry())
    worker.install_signal_handlers(asyncio.get_running_loop())
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: worker.state is WorkerState.RUNNING)

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(task, timeout=2)
    assert worker.state is WorkerState.STOPPED
    asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
    asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
