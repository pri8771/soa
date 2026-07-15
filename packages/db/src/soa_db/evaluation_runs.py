"""Durable, tenant-scoped evaluation runs and promotion evidence."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class EvaluationRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationRun(
    UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, VersionedMixin, Base
):
    __tablename__ = "evaluation_runs"

    stream_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    predictions: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    report: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    gate_result: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    safe_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class EvaluationRunRepository(ScopedRepository[EvaluationRun]):
    model = EvaluationRun

    async def latest_for_stream(self, stream_id: uuid.UUID) -> list[EvaluationRun]:
        stmt = (
            self._scoped_select()
            .where(EvaluationRun.stream_id == stream_id)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def passed_for_candidate(
        self, stream_id: uuid.UUID, candidate_fingerprint: str
    ) -> EvaluationRun | None:
        stmt = (
            self._scoped_select()
            .where(
                EvaluationRun.stream_id == stream_id,
                EvaluationRun.candidate_fingerprint == candidate_fingerprint,
                EvaluationRun.state == EvaluationRunState.SUCCEEDED,
            )
            .order_by(EvaluationRun.finished_at.desc(), EvaluationRun.id.desc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return next((row for row in rows if (row.gate_result or {}).get("passed") is True), None)


async def create_evaluation_run(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    stream_id: uuid.UUID,
    candidate_fingerprint: str,
    dataset_version_id: uuid.UUID,
    predictions: dict[str, Any],
    baseline_run_id: uuid.UUID | None,
    actor_id: str,
) -> EvaluationRun:
    if len(candidate_fingerprint) != 64:
        raise ValueError("candidate fingerprint must be a SHA-256 digest")
    run = EvaluationRunRepository(session, context).add(
        EvaluationRun(
            stream_id=stream_id,
            candidate_fingerprint=candidate_fingerprint,
            dataset_version_id=dataset_version_id,
            predictions=predictions,
            baseline_run_id=baseline_run_id,
            created_by=actor_id,
        )
    )
    await session.flush()
    return run


async def start_evaluation_run(run: EvaluationRun) -> None:
    if run.state not in (EvaluationRunState.PENDING, EvaluationRunState.RUNNING):
        raise ValueError(f"evaluation run cannot start from {run.state!r}")
    run.state = EvaluationRunState.RUNNING
    run.started_at = run.started_at or utcnow()


async def finish_evaluation_run(
    run: EvaluationRun,
    *,
    report: dict[str, Any],
    checkpoint: dict[str, Any],
    gate_result: dict[str, Any],
) -> None:
    run.report = report
    run.checkpoint = checkpoint
    run.gate_result = gate_result
    run.state = EvaluationRunState.SUCCEEDED
    run.finished_at = utcnow()


__all__ = [
    "EvaluationRun",
    "EvaluationRunRepository",
    "EvaluationRunState",
    "create_evaluation_run",
    "finish_evaluation_run",
    "start_evaluation_run",
]
