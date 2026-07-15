import asyncio
import os
import signal

import soa_worker
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import Environment, WorkerSettings
from soa_worker.worker import JobFailureResult, Worker, WorkerState

FAST = WorkerSettings(
    environment=Environment.TEST,
    poll_interval_seconds=0.01,
    heartbeat_interval_seconds=0.01,
)
CONCURRENT = WorkerSettings(
    environment=Environment.TEST,
    poll_interval_seconds=0.01,
    heartbeat_interval_seconds=0.01,
    max_concurrency=2,
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
    assert metadata["max_concurrency"] == 1

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


async def test_worker_runs_only_the_configured_number_of_jobs_concurrently() -> None:
    registry = HandlerRegistry()
    active = 0
    maximum_active = 0

    @registry.register("bounded.job")
    async def bounded_job(_job: JobEnvelope) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.03)
        active -= 1

    queue = [JobEnvelope(job_type="bounded.job") for _ in range(6)]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(CONCURRENT, registry, fetch_job=fetch)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: worker.jobs_completed == 6)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)

    assert maximum_active == 2
    assert worker.active_jobs == ()


async def test_every_concurrent_job_lease_is_heartbeated() -> None:
    registry = HandlerRegistry()
    release = asyncio.Event()
    started = 0
    heartbeated: set[str] = set()

    @registry.register("lease.job")
    async def lease_job(_job: JobEnvelope) -> None:
        nonlocal started
        started += 1
        await release.wait()

    queue = [
        JobEnvelope(job_type="lease.job", correlation_id="lease-a"),
        JobEnvelope(job_type="lease.job", correlation_id="lease-b"),
    ]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    async def heartbeat(job: JobEnvelope) -> None:
        assert job.correlation_id is not None
        heartbeated.add(job.correlation_id)

    worker = Worker(CONCURRENT, registry, fetch_job=fetch, heartbeat_job=heartbeat)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: started == 2)
    await wait_for(lambda: heartbeated == {"lease-a", "lease-b"})
    release.set()
    await wait_for(lambda: worker.jobs_completed == 2)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)


async def test_acknowledgement_outage_does_not_terminate_the_worker() -> None:
    registry = HandlerRegistry()
    acknowledgements = 0

    @registry.register("good.job")
    async def good_job(_job: JobEnvelope) -> None:
        return None

    queue = [JobEnvelope(job_type="good.job"), JobEnvelope(job_type="good.job")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    async def unavailable_ack(_job: JobEnvelope) -> None:
        nonlocal acknowledgements
        acknowledgements += 1
        raise RuntimeError("database unavailable")

    worker = Worker(FAST, registry, fetch_job=fetch, on_job_succeeded=unavailable_ack)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: acknowledgements == 2)
    assert worker.state is WorkerState.RUNNING
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)
    assert worker.jobs_failed == 2


async def test_transient_queue_fetch_failure_does_not_terminate_the_worker() -> None:
    registry = HandlerRegistry()
    attempts = 0

    @registry.register("good.job")
    async def good_job(_job: JobEnvelope) -> None:
        return None

    async def fetch() -> JobEnvelope | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database temporarily unavailable")
        if attempts == 2:
            return JobEnvelope(job_type="good.job")
        return None

    worker = Worker(FAST, registry, fetch_job=fetch)
    task = asyncio.create_task(worker.run())
    await wait_for(lambda: worker.jobs_completed == 1)
    assert worker.state is WorkerState.RUNNING
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)


async def test_terminal_callback_runs_after_failure_acknowledgement() -> None:
    registry = HandlerRegistry()
    callback_finished = asyncio.Event()
    order: list[str] = []

    @registry.register("terminal.job")
    async def terminal_job(_job: JobEnvelope) -> None:
        raise RuntimeError("provider unavailable")

    async def acknowledge(_job: JobEnvelope, _error: BaseException) -> JobFailureResult:
        order.append("queue-committed")
        return JobFailureResult(terminal=True, safe_error="terminal.job failed (RuntimeError)")

    async def fail_domain(_job: JobEnvelope, failure: JobFailureResult) -> None:
        assert failure.terminal is True
        order.append("domain-failed")
        callback_finished.set()

    queue = [JobEnvelope(job_type="terminal.job")]

    async def fetch() -> JobEnvelope | None:
        return queue.pop() if queue else None

    worker = Worker(
        FAST,
        registry,
        fetch_job=fetch,
        on_job_failed=acknowledge,
        on_job_terminal_failure=fail_domain,
    )
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(callback_finished.wait(), timeout=2)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2)
    assert order == ["queue-committed", "domain-failed"]


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
