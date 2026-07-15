"""Database-backed job processing (wires JOB-003/004/005 into the worker).

The Worker run loop (worker.py) is generic: it calls ``fetch_job`` for the
next unit of work and dispatches it through the handler registry. This
module supplies both halves against the real jobs table:

- :meth:`DbJobProcessor.fetch_job` claims one due job with
  ``claim_next_jobs`` (FOR UPDATE SKIP LOCKED) and hands the handler a
  :class:`JobEnvelope`.
- the registered handlers run the PRC-003 orchestrator for the document
  pipeline, then mark the job succeeded — or, on an unexpected error,
  failed with bounded retry / dead-letter (JOB-005).

Processing is strictly sequential (one job at a time per process), so the
processor keeps the single in-flight claim on the instance.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from soa_db import DatabaseSessions
from soa_db.jobs import (
    FailureClass,
    Job,
    claim_next_jobs,
    mark_failed,
    mark_succeeded,
)
from soa_worker.registry import HandlerRegistry, JobEnvelope, JobHandler

logger = logging.getLogger(__name__)

# A payload-only orchestrator entry point (handle_preprocess / handle_stage).
OrchestratorHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


class DbJobProcessor:
    """Claims and completes jobs for the registered orchestrator handlers."""

    def __init__(
        self,
        db: DatabaseSessions,
        handlers: Mapping[str, OrchestratorHandler],
        *,
        worker_id: str | None = None,
    ) -> None:
        self._db = db
        self._handlers = dict(handlers)
        self._worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self._current_job_id: uuid.UUID | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def fetch_job(self) -> JobEnvelope | None:
        async with self._db.session_scope() as session:
            jobs = await claim_next_jobs(
                session,
                worker_id=self._worker_id,
                limit=1,
                job_types=list(self._handlers),
            )
            if not jobs:
                return None
            job = jobs[0]
            self._current_job_id = job.id
            return JobEnvelope(
                job_type=job.job_type,
                payload=dict(job.payload),
                correlation_id=getattr(job, "correlation_id", None),
            )

    def build_registry(self) -> HandlerRegistry:
        registry = HandlerRegistry()
        for job_type, orchestrator_handler in self._handlers.items():
            registry.register(job_type)(self._wrap(orchestrator_handler))
        return registry

    def _wrap(self, orchestrator_handler: OrchestratorHandler) -> JobHandler:
        async def handler(envelope: JobEnvelope) -> None:
            try:
                await orchestrator_handler(envelope.payload)
            except Exception as exc:  # infra/unexpected — the orchestrator
                # handles document/stage failures internally, so reaching
                # here means the job itself could not run.
                await self._complete_failed(f"{type(exc).__name__}: {exc}")
                raise
            else:
                await self._complete_succeeded()

        return handler

    async def _complete_succeeded(self) -> None:
        job_id = self._current_job_id
        if job_id is None:
            return
        async with self._db.session_scope() as session:
            job = await session.get(Job, job_id)
            if job is not None:
                mark_succeeded(job, worker_id=self._worker_id)
        self._current_job_id = None

    async def _complete_failed(self, error: str) -> None:
        job_id = self._current_job_id
        if job_id is None:
            return
        async with self._db.session_scope() as session:
            job = await session.get(Job, job_id)
            if job is not None:
                mark_failed(
                    job,
                    worker_id=self._worker_id,
                    error=error,
                    failure_class=FailureClass.TRANSIENT,
                )
        self._current_job_id = None
