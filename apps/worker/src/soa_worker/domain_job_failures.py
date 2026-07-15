"""Fail tenant domain records when their durable queue intent is terminal.

Queue acknowledgement and domain failure deliberately use separate
transactions. A crash between them is repaired by the idempotent reconciler,
which walks durable dead-letter jobs and re-applies the scoped transition.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select

from soa_db import DatabaseSessions
from soa_db.audit import ActorType, record_audit_event
from soa_db.data_export_jobs import DataExportJob, DataExportJobRepository, DataExportState
from soa_db.deletion_requests import (
    DOCUMENT_DELETION_JOB_TYPE,
    DeletionRequest,
    DeletionRequestRepository,
    DeletionRequestState,
)
from soa_db.evaluation_runs import EvaluationRun, EvaluationRunRepository, EvaluationRunState
from soa_db.jobs import Job, JobStatus
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow
from soa_worker.registry import JobEnvelope
from soa_worker.worker import JobFailureResult

logger = logging.getLogger(__name__)

EVALUATION_JOB_TYPE = "evaluation.run"
DATA_EXPORT_JOB_TYPE = "data_export.build"
DOMAIN_JOB_TYPES = frozenset(
    (EVALUATION_JOB_TYPE, DATA_EXPORT_JOB_TYPE, DOCUMENT_DELETION_JOB_TYPE)
)


@dataclass(frozen=True)
class _DomainTarget:
    kind: str
    organization_id: uuid.UUID
    record_id: uuid.UUID


class DomainJobFailureCoordinator:
    """Post-commit terminal callback plus crash-recovery reconciliation."""

    def __init__(self, db: DatabaseSessions) -> None:
        self._db = db
        # In-memory pagination keeps recurring scans bounded without needing a
        # queue-schema migration. Restarting safely begins again at the oldest
        # dead letter; transitions are idempotent.
        self._cursor: tuple[datetime, uuid.UUID] | None = None

    async def handle(self, envelope: JobEnvelope, failure: JobFailureResult) -> None:
        """Persist a terminal queue result in its tenant-owned aggregate."""
        if not failure.terminal:
            return
        target = self._target(envelope)
        if target is None:
            return
        await self._mark_failed(
            target,
            safe_error=failure.safe_error,
            correlation_id=envelope.correlation_id,
        )

    async def reconcile(self, *, limit: int = 500) -> int:
        """Repair pending/running records whose queue jobs already died.

        One bounded page is scanned per invocation. The cursor advances across
        calls and wraps after the newest row, so old stale records cannot be
        permanently hidden behind already-reconciled dead letters.
        """
        if limit < 1:
            raise ValueError("reconciliation limit must be positive")
        async with self._db.session_scope() as session:
            statement = (
                select(Job)
                .where(
                    Job.status == JobStatus.DEAD_LETTER,
                    Job.job_type.in_(DOMAIN_JOB_TYPES),
                    Job.finished_at.is_not(None),
                )
                .order_by(Job.finished_at, Job.id)
                .limit(limit)
            )
            if self._cursor is not None:
                finished_at, job_id = self._cursor
                statement = statement.where(
                    or_(
                        Job.finished_at > finished_at,
                        and_(Job.finished_at == finished_at, Job.id > job_id),
                    )
                )
            rows = list((await session.execute(statement)).scalars().all())

        if not rows:
            self._cursor = None
            return 0
        last = rows[-1]
        if last.finished_at is None:  # narrowed by the query; defensive for type checking
            return 0
        self._cursor = (last.finished_at, last.id)

        changed = 0
        for job in rows:
            envelope = JobEnvelope(
                job_id=job.id,
                job_type=job.job_type,
                payload=dict(job.payload),
                correlation_id=job.correlation_id,
                organization_id=job.organization_id,
            )
            target = self._target(envelope)
            if target is None:
                continue
            safe_error = job.last_error or f"{job.job_type} exhausted its retry budget"
            if await self._mark_failed(
                target,
                safe_error=safe_error,
                correlation_id=job.correlation_id,
            ):
                changed += 1
        return changed

    def _target(self, envelope: JobEnvelope) -> _DomainTarget | None:
        if envelope.job_type == EVALUATION_JOB_TYPE:
            kind = "evaluation"
            id_key = "evaluation_run_id"
        elif envelope.job_type == DATA_EXPORT_JOB_TYPE:
            kind = "data_export"
            id_key = "data_export_id"
        elif envelope.job_type == DOCUMENT_DELETION_JOB_TYPE:
            kind = "document_deletion"
            id_key = "deletion_request_id"
        else:
            return None

        organization_id = envelope.organization_id
        if organization_id is None:
            logger.error(
                "terminal domain job has no trusted organization",
                extra={"job_type": envelope.job_type, "job_id": str(envelope.job_id)},
            )
            return None
        raw_payload_organization = envelope.payload.get("organization_id")
        if raw_payload_organization is not None:
            try:
                payload_organization = uuid.UUID(str(raw_payload_organization))
            except ValueError:
                logger.error(
                    "terminal domain job has an invalid payload organization",
                    extra={"job_type": envelope.job_type, "job_id": str(envelope.job_id)},
                )
                return None
            if payload_organization != organization_id:
                logger.error(
                    "terminal domain job tenant mismatch",
                    extra={"job_type": envelope.job_type, "job_id": str(envelope.job_id)},
                )
                return None
        try:
            record_id = uuid.UUID(str(envelope.payload[id_key]))
        except (KeyError, ValueError):
            logger.error(
                "terminal domain job has no valid target identifier",
                extra={"job_type": envelope.job_type, "job_id": str(envelope.job_id)},
            )
            return None
        return _DomainTarget(
            kind=kind,
            organization_id=organization_id,
            record_id=record_id,
        )

    async def _mark_failed(
        self,
        target: _DomainTarget,
        *,
        safe_error: str,
        correlation_id: str | None,
    ) -> bool:
        context = OrganizationContext(organization_id=target.organization_id)
        message = (safe_error or "durable queue job failed")[:500]
        async with self._db.session_scope() as session:
            await bind_tenant(session, target.organization_id)
            record: EvaluationRun | DataExportJob | DeletionRequest | None
            if target.kind == "evaluation":
                record = await EvaluationRunRepository(session, context).get(
                    target.record_id, for_update=True
                )
                if record is None or record.state not in (
                    EvaluationRunState.PENDING,
                    EvaluationRunState.RUNNING,
                ):
                    return False
                record.state = EvaluationRunState.FAILED
                action = "evaluation.failed"
                target_type = "evaluation_run"
            elif target.kind == "data_export":
                record = await DataExportJobRepository(session, context).get(
                    target.record_id, for_update=True
                )
                if record is None or record.state not in (
                    DataExportState.PENDING,
                    DataExportState.RUNNING,
                ):
                    return False
                record.state = DataExportState.FAILED
                action = "data_export.failed"
                target_type = "data_export_job"
            else:
                record = await DeletionRequestRepository(session, context).get(
                    target.record_id, for_update=True
                )
                if record is None or record.state not in (
                    DeletionRequestState.APPROVED,
                    DeletionRequestState.RUNNING,
                    DeletionRequestState.FAILED,
                ):
                    return False
                record.state = DeletionRequestState.FAILED
                action = "document.deletion_failed"
                target_type = "document"
            record.safe_error = message
            if isinstance(record, EvaluationRun | DataExportJob):
                record.finished_at = utcnow()
            target_id = (
                str(record.document_id)
                if isinstance(record, DeletionRequest)
                else str(target.record_id)
            )
            await record_audit_event(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id="worker:terminal-failure",
                action=action,
                target_type=target_type,
                target_id=target_id,
                organization_id=target.organization_id,
                correlation_id=correlation_id,
                summary={"safe_error": message},
            )
        return True


__all__ = [
    "DATA_EXPORT_JOB_TYPE",
    "DOMAIN_JOB_TYPES",
    "EVALUATION_JOB_TYPE",
    "DomainJobFailureCoordinator",
]
