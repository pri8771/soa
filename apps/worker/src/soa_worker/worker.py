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
from pathlib import Path
from typing import Any

import soa_worker
from soa_config.logging import correlation_context
from soa_config.telemetry import Telemetry
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
        telemetry: Telemetry | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._fetch_job = fetch_job
        self._telemetry = telemetry if telemetry is not None else Telemetry.noop()
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
        # Records process liveness locally. The DB-backed claim loop
        # (claim_next_jobs / heartbeat / recover_expired_locks from
        # soa_db.jobs, JOB-003/004) is wired into main.py when the first
        # real job handlers land with the ING/PRC epics — until then the
        # worker has nothing to execute and polls idle by design.
        while True:
            self.last_heartbeat_at = time.monotonic()
            self._touch_liveness_file()
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
        self.active_job = job
        with correlation_context(job.correlation_id):
            try:
                handler = self._registry.resolve(job.job_type)
                with self._telemetry.span(
                    f"job {job.job_type}",
                    attributes={"soa.job_type": job.job_type},
                ):
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
