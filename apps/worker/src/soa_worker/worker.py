"""Durable bounded-concurrency worker with graceful drain and heartbeats.

The deployment chooses a small, explicit concurrency budget. Shutdown never
abandons an active handler: a stop request prevents new claims, lets every
active handler finish, records final state, and exits.
"""

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import soa_worker
from soa_config.logging import correlation_context
from soa_config.telemetry import Telemetry
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)

FetchJob = Callable[[], Awaitable[JobEnvelope | None]]
JobSucceeded = Callable[[JobEnvelope], Awaitable[None]]


@dataclass(frozen=True)
class JobFailureResult:
    """Post-commit queue disposition returned by a failure acknowledgement."""

    terminal: bool
    safe_error: str


JobFailed = Callable[[JobEnvelope, BaseException], Awaitable[JobFailureResult | None]]
JobTerminalFailure = Callable[[JobEnvelope, JobFailureResult], Awaitable[None]]
HeartbeatJob = Callable[[JobEnvelope], Awaitable[None]]


class WorkerState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        registry: HandlerRegistry,
        fetch_job: FetchJob | None = None,
        telemetry: Telemetry | None = None,
        on_job_succeeded: JobSucceeded | None = None,
        on_job_failed: JobFailed | None = None,
        on_job_terminal_failure: JobTerminalFailure | None = None,
        heartbeat_job: HeartbeatJob | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._fetch_job = fetch_job
        self._telemetry = telemetry if telemetry is not None else Telemetry.noop()
        self._on_job_succeeded = on_job_succeeded
        self._on_job_failed = on_job_failed
        self._on_job_terminal_failure = on_job_terminal_failure
        self._heartbeat_job = heartbeat_job
        self._stop_event = asyncio.Event()
        self.state = WorkerState.CREATED
        self.jobs_completed = 0
        self.jobs_failed = 0
        self._active_jobs: dict[asyncio.Task[None], JobEnvelope] = {}
        self.last_heartbeat_at: float | None = None

    @property
    def active_job(self) -> JobEnvelope | None:
        """Backward-compatible view of the first active job, if any."""
        return next(iter(self._active_jobs.values()), None)

    @property
    def active_jobs(self) -> tuple[JobEnvelope, ...]:
        return tuple(self._active_jobs.values())

    def metadata(self) -> dict[str, Any]:
        return {
            "service": self._settings.service_name,
            "version": soa_worker.__version__,
            "environment": self._settings.environment.value,
            "registered_job_types": self._registry.registered_types,
            "max_concurrency": self._settings.max_concurrency,
        }

    def request_stop(self) -> None:
        logger.info("worker stop requested")
        self._stop_event.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.request_stop)

    async def _heartbeat_loop(self) -> None:
        # Records process liveness locally and renews the active durable
        # queue lease. Lock recovery and terminal-domain reconciliation
        # run through the database queue adapter.
        while True:
            self.last_heartbeat_at = time.monotonic()
            self._touch_liveness_file()
            if self._heartbeat_job is not None:
                for job in self.active_jobs:
                    try:
                        await self._heartbeat_job(job)
                    except Exception:
                        logger.exception(
                            "could not heartbeat active job",
                            extra={"job_type": job.job_type},
                        )
            logger.debug("worker heartbeat")
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)

    def _touch_liveness_file(self) -> None:
        # Container liveness (REL-001): write the wall-clock beat so an
        # out-of-process HEALTHCHECK can tell a running loop from a hung
        # one. Best-effort — a transient write failure must not crash the
        # worker; the file simply goes stale and the probe reacts.
        path = self._settings.liveness_file
        if not path:
            return
        try:
            Path(path).write_text(f"{time.time():.3f}\n", encoding="utf-8")
        except OSError:
            logger.warning("could not write worker liveness file", extra={"path": path})

    async def _wait_for_stop_or_interval(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._settings.poll_interval_seconds,
            )
        except TimeoutError:
            pass

    async def _run_job(self, job: JobEnvelope) -> None:
        with correlation_context(job.correlation_id):
            try:
                handler = self._registry.resolve(job.job_type)
                with self._telemetry.span(
                    f"job {job.job_type}",
                    attributes={"soa.job_type": job.job_type},
                ):
                    await handler(job)
            except Exception as error:
                self.jobs_failed += 1
                logger.exception("job handler failed", extra={"job_type": job.job_type})
                failure_result: JobFailureResult | None = None
                if self._on_job_failed is not None:
                    # Durable queues commit their retry/dead-letter transition
                    # before returning this disposition. Domain failure state
                    # is consequently persisted in a separate transaction.
                    try:
                        failure_result = await self._on_job_failed(job, error)
                    except Exception:
                        # Leave the durable lease to expire and be recovered.
                        # An acknowledgement outage must not terminate every
                        # other handler in this worker process.
                        logger.exception(
                            "could not acknowledge failed job",
                            extra={"job_type": job.job_type},
                        )
                if (
                    failure_result is not None
                    and failure_result.terminal
                    and self._on_job_terminal_failure is not None
                ):
                    try:
                        await self._on_job_terminal_failure(job, failure_result)
                    except Exception:
                        # The queue is already durably dead-lettered. A
                        # periodic reconciler retries this idempotent domain
                        # transition without resurrecting the queue job.
                        logger.exception(
                            "terminal job domain callback failed",
                            extra={"job_type": job.job_type},
                        )
            else:
                try:
                    if self._on_job_succeeded is not None:
                        await self._on_job_succeeded(job)
                except Exception:
                    # The handler's domain transaction succeeded, but the
                    # queue acknowledgement did not. Lock recovery redelivers
                    # the idempotent handler; the process keeps serving work.
                    self.jobs_failed += 1
                    logger.exception(
                        "could not acknowledge completed job",
                        extra={"job_type": job.job_type},
                    )
                else:
                    self.jobs_completed += 1
                    logger.info("job completed", extra={"job_type": job.job_type})

    async def _reap_finished(self, tasks: set[asyncio.Task[None]]) -> None:
        finished = {task for task in tasks if task.done()}
        for task in finished:
            tasks.remove(task)
            self._active_jobs.pop(task, None)
            try:
                await task
            except asyncio.CancelledError:
                logger.warning("worker job task was cancelled unexpectedly")
            except BaseException:
                # _run_job contains ordinary handler/acknowledgement errors.
                # Keep this final boundary for programming-level exceptions
                # so one task cannot strand every other durable lease.
                logger.exception("worker job task terminated unexpectedly")

    async def run(self) -> None:
        """Run until stop is requested; safe to await from an entry point."""
        self.state = WorkerState.STARTING
        logger.info("worker starting", extra=self.metadata())
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        active_tasks: set[asyncio.Task[None]] = set()
        self.state = WorkerState.RUNNING
        try:
            while not self._stop_event.is_set():
                while (
                    not self._stop_event.is_set()
                    and len(active_tasks) < self._settings.max_concurrency
                    and self._fetch_job is not None
                ):
                    try:
                        job = await self._fetch_job()
                    except Exception:
                        self._telemetry.add_to_counter("soa.worker.queue_fetch_errors")
                        logger.exception("could not fetch a durable job")
                        # A database/network outage is retryable process-wide;
                        # keep heartbeating any already-active leases and retry
                        # after the bounded poll interval.
                        await self._wait_for_stop_or_interval()
                        break
                    if job is None:
                        break
                    task = asyncio.create_task(self._run_job(job))
                    active_tasks.add(task)
                    self._active_jobs[task] = job

                await self._reap_finished(active_tasks)
                if self._stop_event.is_set():
                    break
                if not active_tasks:
                    await self._wait_for_stop_or_interval()
                    continue
                # Wake quickly for either a completed slot or a stop request;
                # the bounded timeout also lets a newly enqueued job fill any
                # free slot when the last fetch found an empty queue.
                await asyncio.wait(
                    active_tasks,
                    timeout=self._settings.poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await self._reap_finished(active_tasks)
        finally:
            self.state = WorkerState.STOPPING
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
                await self._reap_finished(active_tasks)
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            self.state = WorkerState.STOPPED
            logger.info(
                "worker stopped",
                extra={
                    "jobs_completed": self.jobs_completed,
                    "jobs_failed": self.jobs_failed,
                },
            )
