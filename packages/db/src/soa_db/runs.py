"""Processing-run and stage-run models (PRC-001).

A ProcessingRun is one complete pass of a document through the pipeline
under ONE fixed configuration: it records the pinned stream version, the
resolved-configuration fingerprint (CFG-006), and the input content
fingerprint, so any result can be traced to exactly what produced it.
Reprocessing NEVER mutates an old run — it creates a new one with the
next run number.

A StageRun is one attempt of one stage inside a run. The database
enforces idempotency: (run, stage, attempt) is unique, so a duplicate
execution of the same attempt cannot record twice. A stage attempt that
succeeded is immutable — retries are new attempts, corrections are new
runs. Errors are stored as SAFE, classified strings (never raw provider
responses), with a failure class the orchestrator uses for retry
decisions.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, Text, UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageFailureClass(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    RunState.RUNNING.value: frozenset(
        {RunState.SUCCEEDED.value, RunState.FAILED.value, RunState.CANCELLED.value}
    ),
    RunState.SUCCEEDED.value: frozenset(),
    RunState.FAILED.value: frozenset(),
    RunState.CANCELLED.value: frozenset(),
}


class RunStateError(Exception):
    pass


class StageImmutableError(Exception):
    def __init__(self, stage_run_id: object) -> None:
        super().__init__(
            f"stage run {stage_run_id} has succeeded and is immutable — "
            "retries are new attempts, corrections are new runs"
        )


class ProcessingRun(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "processing_runs"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_number: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=RunState.RUNNING.value)
    #: Configuration snapshot: the stream version this run was pinned to
    #: and the resolver's deterministic fingerprint of the resolved config.
    stream_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instruction_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    confidence_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    provider_policy_version_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: Opaque secret-store reference, never the credential value.
    provider_credential_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Hash of every immutable id/reference above plus config_fingerprint.
    execution_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: What actually executed: renderer, native/OCR engines, adapter,
    #: model, and a sanitized endpoint digest. Set once by extraction.
    runtime_provenance: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    runtime_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Fingerprint of the input the run consumed (the original's SHA-256).
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    #: Rolled-up metrics, updated as stages finish.
    total_latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    total_cost_cents: Mapped[int] = mapped_column(nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("document_id", "run_number"),
        Index("ix_processing_runs_org_state", "organization_id", "state"),
    )


class StageRun(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "stage_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=StageState.RUNNING.value)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    cost_cents: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Classified, display-safe error — never a raw provider response.
    safe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Small structured results: artifact ids, counts — never payloads.
    output_summary: Mapped[dict[str, Any]] = mapped_column(
        PORTABLE_JSON, nullable=False, default=dict
    )

    __table_args__ = (
        # Idempotency at the database: one row per (run, stage, attempt).
        UniqueConstraint("run_id", "stage", "attempt"),
    )


@event.listens_for(Session, "before_flush")
def _freeze_terminal_rows(session: Session, _ctx: object, _instances: object) -> None:
    from sqlalchemy import inspect as sa_inspect

    for entity in session.dirty:
        if not session.is_modified(entity):
            continue
        if isinstance(entity, StageRun):
            history = sa_inspect(entity).attrs["state"].history
            previous = str(history.deleted[0]) if history.deleted else entity.state
            if previous == StageState.SUCCEEDED.value:
                raise StageImmutableError(entity.id)
        elif isinstance(entity, ProcessingRun):
            history = sa_inspect(entity).attrs["state"].history
            if not history.has_changes() or not history.deleted:
                continue
            old = str(history.deleted[0])
            new = str(entity.state)
            if old != new and new not in _RUN_TRANSITIONS.get(old, frozenset()):
                raise RunStateError(f"run {entity.id}: transition {old!r} -> {new!r} not allowed")


class ProcessingRunRepository(ScopedRepository[ProcessingRun]):
    model = ProcessingRun

    async def list_for_document(self, document_id: uuid.UUID) -> list[ProcessingRun]:
        stmt = (
            self._scoped_select()
            .where(ProcessingRun.document_id == document_id)
            .order_by(ProcessingRun.run_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class StageRunRepository(ScopedRepository[StageRun]):
    model = StageRun

    async def list_for_run(self, run_id: uuid.UUID) -> list[StageRun]:
        stmt = (
            self._scoped_select()
            .where(StageRun.run_id == run_id)
            .order_by(StageRun.stage, StageRun.attempt)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_for_runs(self, run_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[StageRun]]:
        """Fetch ordered stage attempts for many runs in one scoped query."""

        grouped: dict[uuid.UUID, list[StageRun]] = {run_id: [] for run_id in run_ids}
        if not run_ids:
            return grouped
        stmt = (
            self._scoped_select()
            .where(StageRun.run_id.in_(run_ids))
            .order_by(StageRun.run_id, StageRun.stage, StageRun.attempt)
        )
        for stage in (await self._session.execute(stmt)).scalars().all():
            grouped.setdefault(stage.run_id, []).append(stage)
        return grouped

    async def latest_attempt(self, run_id: uuid.UUID, stage: str) -> int:
        from sqlalchemy import func, select

        stmt = select(func.max(StageRun.attempt)).where(
            StageRun.run_id == run_id, StageRun.stage == stage
        )
        return int((await self._session.execute(stmt)).scalar_one() or 0)


async def start_run(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    input_sha256: str,
    stream_version_id: uuid.UUID | None,
    config_fingerprint: str | None,
    triggered_by: str,
    instruction_version_id: uuid.UUID | None = None,
    confidence_policy_version_id: uuid.UUID | None = None,
    provider_policy_version_id: uuid.UUID | None = None,
    provider_credential_ref: str | None = None,
    execution_fingerprint: str | None = None,
    reason: str | None = None,
) -> ProcessingRun:
    """Start a new run. Reprocessing calls this again: run numbers only
    ever grow, prior runs stay untouched."""
    existing = await ProcessingRunRepository(session, context).list_for_document(document_id)
    run = ProcessingRunRepository(session, context).add(
        ProcessingRun(
            document_id=document_id,
            run_number=(existing[-1].run_number + 1) if existing else 1,
            input_sha256=input_sha256,
            stream_version_id=stream_version_id,
            config_fingerprint=config_fingerprint,
            instruction_version_id=instruction_version_id,
            confidence_policy_version_id=confidence_policy_version_id,
            provider_policy_version_id=provider_policy_version_id,
            provider_credential_ref=provider_credential_ref,
            execution_fingerprint=execution_fingerprint,
            triggered_by=triggered_by,
            reason=reason,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=triggered_by,
        action="processing_run.started",
        target_type="processing_run",
        target_id=str(run.id),
        organization_id=context.organization_id,
        summary={
            "document_id": str(document_id),
            "run_number": run.run_number,
            "config_fingerprint": config_fingerprint,
            "execution_fingerprint": execution_fingerprint,
            "reason": reason,
        },
    )
    return run


async def start_stage(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    run: ProcessingRun,
    stage: str,
    provider: str | None = None,
) -> StageRun:
    """Open the next attempt of a stage. The unique constraint refuses a
    duplicate execution of the same attempt at the database."""
    if run.state != RunState.RUNNING.value:
        raise RunStateError(f"run {run.id} is {run.state}; stages only start on running runs")
    repo = StageRunRepository(session, context)
    attempt = await repo.latest_attempt(run.id, stage) + 1
    stage_run = repo.add(StageRun(run_id=run.id, stage=stage, attempt=attempt, provider=provider))
    await session.flush()
    return stage_run


async def complete_stage(
    session: AsyncSession,
    *,
    run: ProcessingRun,
    stage_run: StageRun,
    latency_ms: int,
    cost_cents: int = 0,
    output_summary: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> StageRun:
    if stage_run.state != StageState.RUNNING.value:
        raise RunStateError(f"stage run {stage_run.id} is {stage_run.state}; cannot complete")
    stage_run.state = StageState.SUCCEEDED.value
    stage_run.finished_at = now or utcnow()
    stage_run.latency_ms = latency_ms
    stage_run.cost_cents = cost_cents
    stage_run.output_summary = dict(output_summary or {})
    run.total_latency_ms += latency_ms
    run.total_cost_cents += cost_cents
    await session.flush()
    return stage_run


async def fail_stage(
    session: AsyncSession,
    *,
    stage_run: StageRun,
    run: ProcessingRun | None = None,
    safe_error: str,
    failure_class: StageFailureClass,
    latency_ms: int | None = None,
    cost_cents: int = 0,
    now: datetime | None = None,
) -> StageRun:
    if stage_run.state != StageState.RUNNING.value:
        raise RunStateError(f"stage run {stage_run.id} is {stage_run.state}; cannot fail")
    if cost_cents < 0:
        raise RunStateError("stage cost cannot be negative")
    stage_run.state = StageState.FAILED.value
    stage_run.finished_at = now or utcnow()
    stage_run.latency_ms = latency_ms
    stage_run.safe_error = safe_error[:500]
    stage_run.failure_class = failure_class.value
    stage_run.cost_cents = cost_cents
    if run is not None:
        if run.id != stage_run.run_id:
            raise RunStateError("stage run belongs to a different processing run")
        run.total_cost_cents += cost_cents
    await session.flush()
    return stage_run


async def finish_run(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    run: ProcessingRun,
    state: RunState,
    actor_id: str,
    now: datetime | None = None,
) -> ProcessingRun:
    if state == RunState.RUNNING:
        raise RunStateError("finish_run requires a terminal state")
    run.state = state.value  # the flush guard validates the transition
    run.finished_at = now or utcnow()
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="processing_run.finished",
        target_type="processing_run",
        target_id=str(run.id),
        organization_id=context.organization_id,
        summary={
            "state": state.value,
            "total_latency_ms": run.total_latency_ms,
            "total_cost_cents": run.total_cost_cents,
        },
    )
    return run
