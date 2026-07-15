"""Execute and boundedly redrive durable external-resource cleanup intents."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select

from soa_config import SecretReference, SecretStore
from soa_db import DatabaseSessions
from soa_db.external_cleanup import (
    EXTERNAL_CLEANUP_JOB_TYPE,
    MAX_CLEANUP_DISPATCHES,
    ExternalCleanupIntentRepository,
    ExternalCleanupState,
    ExternalResourceType,
    safe_cleanup_error,
    validate_resource_locator,
)
from soa_db.jobs import Job, JobStatus, enqueue_job
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow
from soa_storage import ObjectNotFoundError, ObjectStore
from soa_worker.registry import HandlerRegistry, JobEnvelope

logger = logging.getLogger(__name__)


class ExternalCleanupRetryError(RuntimeError):
    """Safe retry signal that never copies a provider diagnostic or locator."""


def _target(job: JobEnvelope) -> tuple[uuid.UUID, uuid.UUID, int]:
    organization_id = job.organization_id
    if organization_id is None:
        raise ValueError("external cleanup requires a trusted organization id")
    try:
        payload_organization_id = uuid.UUID(str(job.payload["organization_id"]))
        intent_id = uuid.UUID(str(job.payload["cleanup_intent_id"]))
        dispatch = int(job.payload["dispatch"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError("external cleanup payload is invalid") from error
    if payload_organization_id != organization_id:
        raise ValueError("external cleanup tenant payload does not match its queue envelope")
    if not 1 <= dispatch <= MAX_CLEANUP_DISPATCHES:
        raise ValueError("external cleanup payload has an invalid dispatch generation")
    return organization_id, intent_id, dispatch


async def _record_attempt(
    db: DatabaseSessions,
    *,
    organization_id: uuid.UUID,
    intent_id: uuid.UUID,
    error: BaseException | None,
) -> None:
    context = OrganizationContext(organization_id=organization_id)
    async with db.session_scope() as session:
        await bind_tenant(session, organization_id)
        intent = await ExternalCleanupIntentRepository(session, context).get(
            intent_id, for_update=True
        )
        if intent is None:
            raise ValueError("external cleanup intent does not belong to the job tenant")
        if intent.state != ExternalCleanupState.PENDING.value:
            return
        intent.attempts += 1
        if error is None:
            intent.state = ExternalCleanupState.COMPLETED.value
            intent.completed_at = utcnow()
            intent.last_error = None
            intent.resource_locator = None
        else:
            intent.last_error = safe_cleanup_error(
                ExternalResourceType(intent.resource_type), error
            )


def register_external_cleanup_handler(
    registry: HandlerRegistry,
    db: DatabaseSessions,
    object_store: ObjectStore,
    secret_store: SecretStore,
) -> None:
    """Register an idempotent handler whose payload never contains a locator."""

    @registry.register(EXTERNAL_CLEANUP_JOB_TYPE)
    async def cleanup_external_resource(job: JobEnvelope) -> None:
        organization_id, intent_id, dispatch = _target(job)
        context = OrganizationContext(organization_id=organization_id)

        # Read and authorize metadata in a short transaction. External I/O is
        # deliberately outside the row lock; queue lease fencing serializes
        # live handlers, and both deletion operations are idempotent.
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            intent = await ExternalCleanupIntentRepository(session, context).get(intent_id)
            if intent is None:
                raise ValueError("external cleanup intent does not belong to the job tenant")
            if intent.state != ExternalCleanupState.PENDING.value:
                return
            if intent.dispatch_count != dispatch:
                return
            try:
                resource_type = ExternalResourceType(intent.resource_type)
            except ValueError as error:
                raise ValueError("external cleanup intent has an invalid resource type") from error
            locator = intent.resource_locator
            if locator is None:
                raise ValueError("pending external cleanup intent has no resource locator")
            validate_resource_locator(organization_id, resource_type, locator)

        try:
            if resource_type is ExternalResourceType.OBJECT:
                try:
                    await object_store.delete(locator)
                except ObjectNotFoundError:
                    pass
            else:
                await secret_store.revoke(SecretReference.parse(locator))
        except Exception as error:
            diagnostic = safe_cleanup_error(resource_type, error)
            await _record_attempt(
                db,
                organization_id=organization_id,
                intent_id=intent_id,
                error=error,
            )
            raise ExternalCleanupRetryError(diagnostic) from None

        # A crash after external deletion but before this commit redelivers an
        # already-deleted locator and safely reaches the same completed state.
        await _record_attempt(
            db,
            organization_id=organization_id,
            intent_id=intent_id,
            error=None,
        )


class ExternalCleanupCoordinator:
    """Boundedly redrive cleanup jobs that exhausted queue-level retries."""

    def __init__(self, db: DatabaseSessions) -> None:
        self._db = db
        self._cursor: tuple[datetime, uuid.UUID] | None = None

    async def reconcile(self, *, limit: int = 100) -> int:
        """Scan one bounded dead-letter page and redrive each job once.

        A cleanup receives at most three queue dispatches, each with five
        queue attempts. After that the durable intent enters
        ``manual_intervention`` instead of retrying forever.
        """

        if limit < 1:
            raise ValueError("external cleanup reconciliation limit must be positive")
        async with self._db.session_scope() as session:
            statement = (
                select(Job)
                .where(
                    Job.status == JobStatus.DEAD_LETTER,
                    Job.job_type == EXTERNAL_CLEANUP_JOB_TYPE,
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
            jobs = list((await session.execute(statement)).scalars().all())

        if not jobs:
            self._cursor = None
            return 0
        last = jobs[-1]
        if last.finished_at is None:  # narrowed above; defensive for typing
            return 0
        self._cursor = (last.finished_at, last.id)

        changed = 0
        for job in jobs:
            envelope = JobEnvelope(
                job_id=job.id,
                job_type=job.job_type,
                payload=dict(job.payload),
                organization_id=job.organization_id,
                correlation_id=job.correlation_id,
            )
            try:
                if await self._redrive(envelope):
                    changed += 1
            except ValueError:
                # A malformed/tenant-mismatched cleanup job is itself a
                # security diagnostic, never a reason to stop healthy sweeps.
                logger.error(
                    "refused invalid dead-letter external cleanup job",
                    extra={"job_id": str(job.id)},
                )
        return changed

    async def _redrive(self, job: JobEnvelope) -> bool:
        organization_id, intent_id, dispatch = _target(job)
        if job.job_id is None:
            raise ValueError("dead-letter cleanup job has no job id")
        context = OrganizationContext(organization_id=organization_id)
        async with self._db.session_scope() as session:
            await bind_tenant(session, organization_id)
            intent = await ExternalCleanupIntentRepository(session, context).get(
                intent_id, for_update=True
            )
            if intent is None:
                raise ValueError("external cleanup intent does not belong to the job tenant")
            if intent.state != ExternalCleanupState.PENDING.value:
                return False
            if intent.dispatch_count != dispatch:
                return False
            if intent.last_reconciled_job_id == job.job_id:
                return False
            intent.last_reconciled_job_id = job.job_id
            if intent.dispatch_count >= MAX_CLEANUP_DISPATCHES:
                intent.state = ExternalCleanupState.MANUAL_INTERVENTION.value
                intent.last_error = "external cleanup exhausted its automated retry budget"
                return True

            intent.dispatch_count += 1
            await enqueue_job(
                session,
                job_type=EXTERNAL_CLEANUP_JOB_TYPE,
                organization_id=organization_id,
                payload={
                    "organization_id": str(organization_id),
                    "cleanup_intent_id": str(intent.id),
                    "dispatch": intent.dispatch_count,
                },
                dedupe_key=f"external.cleanup:{intent.id}:{intent.dispatch_count}",
                max_attempts=5,
                priority=20,
            )
            return True


__all__ = [
    "ExternalCleanupCoordinator",
    "ExternalCleanupRetryError",
    "register_external_cleanup_handler",
]
