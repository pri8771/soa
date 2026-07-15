"""Processing orchestrator (PRC-003).

Drives a document through the stage sequence with NOTHING held only in
process memory: every stage execution is its own durable job row
(deduped on (run, stage, attempt)), every attempt is a StageRun row,
and the run's configuration snapshot is fixed at intake time (the
preprocess job payload pins the stream version and config fingerprint).
Kill the worker at any point and redelivery resumes exactly where the
database says things stand:

- a stage job whose stage already SUCCEEDED is a no-op that re-enqueues
  the next stage (duplicate messages and crash-after-commit are safe);
- a stage job for a terminal run is dropped;
- a cancelled document finishes its run as cancelled before any more
  work happens.

Stage implementations are pluggable executors (render/extract/… arrive
with PRC-004+); the orchestrator owns selection, retry classification
(retryable failures re-enqueue with backoff up to a bounded attempt
count, terminal failures stop the run), and document state transitions
through the audited PRC-002 service.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import DatabaseSessions
from soa_db.documents import Document, DocumentRepository, DocumentState, transition_document
from soa_db.jobs import enqueue_job, retry_backoff
from soa_db.repository import OrganizationContext
from soa_db.runs import (
    ProcessingRun,
    ProcessingRunRepository,
    RunState,
    StageFailureClass,
    StageRun,
    StageRunRepository,
    complete_stage,
    fail_stage,
    finish_run,
    start_run,
    start_stage,
)
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow

logger = logging.getLogger(__name__)

#: Stage names deliberately equal the document states they occupy.
STAGE_SEQUENCE: tuple[str, ...] = (
    DocumentState.PREPROCESSING.value,
    DocumentState.CLASSIFYING.value,
    DocumentState.SPLITTING.value,
    DocumentState.EXTRACTING.value,
    DocumentState.NORMALIZING.value,
    DocumentState.VALIDATING_DATA.value,
)

STAGE_JOB_TYPE = "document.stage"
ACTOR = "system:orchestrator"


@dataclass(frozen=True)
class StageOutcome:
    output_summary: dict[str, Any] = field(default_factory=dict)
    cost_cents: int = 0
    provider: str | None = None
    #: Only the final stage decides routing: approved or review_required.
    route: str | None = None


class StageExecutionError(Exception):
    """A stage failed. ``retryable`` drives the orchestrator's retry
    classification; the message must be display-safe."""

    def __init__(self, safe_message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(safe_message)


StageExecutor = Callable[
    [AsyncSession, OrganizationContext, ProcessingRun, Document, StageRun],
    Awaitable[StageOutcome],
]
ConfigVerifier = Callable[
    [AsyncSession, OrganizationContext, ProcessingRun],
    Awaitable[Mapping[str, Any]],
]


class Orchestrator:
    def __init__(
        self,
        db: DatabaseSessions,
        executors: Mapping[str, StageExecutor],
        *,
        max_stage_attempts: int = 3,
        config_verifier: ConfigVerifier | None = None,
    ) -> None:
        self._db = db
        self._executors = dict(executors)
        self._max_stage_attempts = max_stage_attempts
        self._config_verifier = config_verifier

    # -- entry: document.preprocess ------------------------------------------

    async def handle_preprocess(self, payload: Mapping[str, Any]) -> None:
        """Open a run for a freshly queued document and enqueue the first
        stage. Duplicate deliveries are no-ops (the document has moved on)."""
        organization_id = uuid.UUID(str(payload["organization_id"]))
        document_id = uuid.UUID(str(payload["document_id"]))
        context = OrganizationContext(organization_id=organization_id)
        async with self._db.session_scope() as session:
            await bind_tenant(session, organization_id)
            document = await DocumentRepository(session, context).get(document_id)
            if document is None:
                logger.warning("preprocess job for unknown document %s", document_id)
                return
            if document.state != DocumentState.QUEUED.value:
                logger.info(
                    "preprocess duplicate/no-op: document %s is %s",
                    document_id,
                    document.state,
                )
                return
            raw_version = payload.get("stream_version_id")
            run = await start_run(
                session,
                context,
                document_id=document_id,
                input_sha256=document.content_sha256,
                stream_version_id=uuid.UUID(str(raw_version)) if raw_version else None,
                config_fingerprint=(
                    str(payload["config_fingerprint"])
                    if payload.get("config_fingerprint")
                    else None
                ),
                triggered_by=ACTOR,
            )
            await self._enqueue_stage(session, context, run, STAGE_SEQUENCE[0], attempt=1)

    # -- per-stage: document.stage --------------------------------------------

    async def handle_stage(self, payload: Mapping[str, Any]) -> None:
        organization_id = uuid.UUID(str(payload["organization_id"]))
        run_id = uuid.UUID(str(payload["run_id"]))
        stage = str(payload["stage"])
        context = OrganizationContext(organization_id=organization_id)

        async with self._db.session_scope() as session:
            await bind_tenant(session, organization_id)
            run = await ProcessingRunRepository(session, context).get(run_id)
            if run is None or run.state != RunState.RUNNING.value:
                logger.info("stage job dropped: run %s is not running", run_id)
                return
            document = await DocumentRepository(session, context).get(run.document_id)
            if document is None:
                return

            # Cancellation wins before any work.
            if document.state == DocumentState.CANCELLED.value:
                await finish_run(
                    session, context, run=run, state=RunState.CANCELLED, actor_id=ACTOR
                )
                return

            stages = await StageRunRepository(session, context).list_for_run(run.id)
            if any(s.stage == stage and s.state == "succeeded" for s in stages):
                # Duplicate message / crash after commit: ensure progress,
                # do not re-execute.
                await self._advance(session, context, run, document, stage, route=None)
                return

            if document.state != stage:
                await transition_document(
                    session,
                    context,
                    document=document,
                    to_state=DocumentState(stage),
                    actor_id=ACTOR,
                )
            stage_run = await start_stage(session, context, run=run, stage=stage)
            executor = self._executors.get(stage)
            started = time.monotonic()
            try:
                if self._config_verifier is not None:
                    try:
                        await self._config_verifier(session, context, run)
                    except ValueError as error:
                        raise StageExecutionError(str(error), retryable=False) from error
                if executor is None:
                    raise StageExecutionError(
                        f"no executor registered for stage {stage!r}", retryable=False
                    )
                outcome = await executor(session, context, run, document, stage_run)
            except StageExecutionError as error:
                await self._record_failure(
                    session,
                    context,
                    run,
                    document,
                    stage_run,
                    error,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                return
            if outcome.provider is not None:
                stage_run.provider = outcome.provider  # before success seals the row
            await complete_stage(
                session,
                run=run,
                stage_run=stage_run,
                latency_ms=int((time.monotonic() - started) * 1000),
                cost_cents=outcome.cost_cents,
                output_summary=outcome.output_summary,
            )
            await self._advance(session, context, run, document, stage, route=outcome.route)

    # -- helpers ---------------------------------------------------------------

    async def _advance(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        completed_stage: str,
        *,
        route: str | None,
    ) -> None:
        index = STAGE_SEQUENCE.index(completed_stage)
        if index + 1 < len(STAGE_SEQUENCE):
            next_stage = STAGE_SEQUENCE[index + 1]
            attempt = await StageRunRepository(session, context).latest_attempt(run.id, next_stage)
            await self._enqueue_stage(session, context, run, next_stage, attempt=attempt + 1)
            return
        # Final stage: route the document and close the run.
        decision = route if route in ("approved", "review_required") else "review_required"
        if document.state == DocumentState.VALIDATING_DATA.value:
            await transition_document(
                session,
                context,
                document=document,
                to_state=DocumentState(decision),
                reason=None if decision == "approved" else "routed to review",
                actor_id=ACTOR,
            )
        await finish_run(session, context, run=run, state=RunState.SUCCEEDED, actor_id=ACTOR)

    async def _enqueue_stage(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        stage: str,
        *,
        attempt: int,
        run_after_seconds: float | None = None,
    ) -> None:
        from datetime import timedelta

        await enqueue_job(
            session,
            job_type=STAGE_JOB_TYPE,
            payload={
                "run_id": str(run.id),
                "organization_id": str(context.organization_id),
                "stage": stage,
            },
            organization_id=context.organization_id,
            # Durable + exactly-once per attempt: duplicates collapse here.
            dedupe_key=f"stage:{run.id}:{stage}:{attempt}",
            run_after=(
                utcnow() + timedelta(seconds=run_after_seconds) if run_after_seconds else None
            ),
        )

    async def _record_failure(
        self,
        session: AsyncSession,
        context: OrganizationContext,
        run: ProcessingRun,
        document: Document,
        stage_run: StageRun,
        error: StageExecutionError,
        *,
        latency_ms: int,
    ) -> None:
        failure_class = (
            StageFailureClass.RETRYABLE if error.retryable else StageFailureClass.TERMINAL
        )
        await fail_stage(
            session,
            stage_run=stage_run,
            safe_error=str(error),
            failure_class=failure_class,
            latency_ms=latency_ms,
        )
        if error.retryable and stage_run.attempt < self._max_stage_attempts:
            backoff = retry_backoff(stage_run.attempt).total_seconds()
            await self._enqueue_stage(
                session,
                context,
                run,
                stage_run.stage,
                attempt=stage_run.attempt + 1,
                run_after_seconds=backoff,
            )
            return
        # Out of road: the run fails; the document parks with a safe reason.
        target = (
            DocumentState.FAILED_RETRYABLE if error.retryable else DocumentState.FAILED_TERMINAL
        )
        await transition_document(
            session,
            context,
            document=document,
            to_state=target,
            reason=f"stage {stage_run.stage} failed: {error}",
            actor_id=ACTOR,
        )
        await finish_run(session, context, run=run, state=RunState.FAILED, actor_id=ACTOR)
