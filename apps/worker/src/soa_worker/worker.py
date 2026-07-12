"""Worker run loop with graceful shutdown and heartbeat skeleton.

The loop is deliberately sequential: one job at a time per worker process.
Shutdown never abandons an active handler — a stop request lets the current
handler finish, then drains, records final state, and exits.
"""

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import soa_worker
from soa_config.logging import correlation_context
from soa_worker.registry import HandlerRegistry, JobEnvelope
from soa_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)

FetchJob = Callable[[], Awaitable[JobEnvelope | None]]


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
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._fetch_job = fetch_job
        self._stop_event = asyncio.Event()
        self.state = WorkerState.CREATED
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.active_job: JobEnvelope | None = None
        self.last_heartbeat_at: float | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "service": self._settings.service_name,
            "version": soa_worker.__version__,
            "environment": self._settings.environment.value,
            "registered_job_types": self._registry.registered_types,
        }

    def request_stop(self) -> None:
        logger.info("worker stop requested")
        self._stop_event.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.request_stop)

    async def _heartbeat_loop(self) -> None:
        # Skeleton: records liveness locally. JOB-004 extends this to renew
        # database job locks so long handlers are not reclaimed mid-run.
        while True:
            self.last_heartbeat_at = time.monotonic()
            logger.debug("worker heartbeat")
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)

    async def _wait_for_stop_or_interval(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._settings.poll_interval_seconds,
            )
        except TimeoutError:
            pass

    async def _run_job(self, job: JobEnvelope) -> None:
        self.active_job = job
        with correlation_context(job.correlation_id):
            try:
                handler = self._registry.resolve(job.job_type)
                await handler(job)
            except Exception:
                self.jobs_failed += 1
                logger.exception("job handler failed", extra={"job_type": job.job_type})
            else:
                self.jobs_completed += 1
                logger.info("job completed", extra={"job_type": job.job_type})
            finally:
                self.active_job = None

    async def run(self) -> None:
        """Run until stop is requested; safe to await from an entry point."""
        self.state = WorkerState.STARTING
        logger.info("worker starting", extra=self.metadata())
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        self.state = WorkerState.RUNNING
        try:
            while not self._stop_event.is_set():
                job = await self._fetch_job() if self._fetch_job is not None else None
                if job is None:
                    await self._wait_for_stop_or_interval()
                    continue
                await self._run_job(job)
        finally:
            self.state = WorkerState.STOPPING
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
