"""Scheduled retention sweep (SEC-008/SEC-010 follow-through).

Until now, nothing aged data automatically — deletion was entirely
operator-triggered (see :mod:`soa_db.deletion_requests`). This coordinator
closes that gap the SAME conservative way the rest of the deletion system
works: it only ever creates a deletion REQUEST, landing a settled document
in ``pending_approval`` once its retention window elapses. It never
approves and never deletes — the existing two-person approval gate (an
approver distinct from the requester, enforced by
:func:`soa_db.deletion_requests.approve_document_deletion`) still stands
between an automatic request and actual erasure. A legal hold or a
non-standard artifact retention class is a human decision this sweep
never overrides; it simply skips those documents.

Two documented simplifications versus the engine's ideal (the
:mod:`soa_db.retention` docstring calls policy-pinning "load bearing"):

- Settlement time is approximated as the document's ``updated_at`` at scan
  time, since nothing pins a ``settled_at`` timestamp today. Settled
  documents are not otherwise mutated, so this holds in practice.
- The policy applied is the organization's CURRENTLY published retention
  policy, not one pinned at settlement. A shorter policy published later
  can therefore make older documents eligible sooner than a true pin
  would allow.

Both are safe simplifications because the worst case they produce is an
EARLIER deletion REQUEST — still gated by human approval, never an
earlier deletion.

Driven by the worker's existing periodic reconcile hook (``main.py``), the
same mechanism :class:`soa_worker.external_cleanup.ExternalCleanupCoordinator`
already uses — no new job type, queue payload, or migration.
"""

import json
import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import DatabaseSessions
from soa_db.artifacts import Artifact, RetentionClass
from soa_db.deletion_requests import (
    DELETION_SETTLED_STATES,
    DeletionLifecycleError,
    DeletionRequest,
    LegalHoldRepository,
    request_document_deletion,
)
from soa_db.documents import Document
from soa_db.repository import OrganizationContext
from soa_db.retention import RetentionPolicy, evaluate_retention
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow

logger = logging.getLogger(__name__)

#: Same system-actor naming convention as other automated worker actions
#: (compare the classify stage's ``triggered_by="manual-route"``).
RETENTION_SCHEDULER_ACTOR = "system:retention-scheduler"


def _database_identifier(session: AsyncSession, value: object) -> object:
    if isinstance(value, uuid.UUID) and session.get_bind().dialect.name == "sqlite":
        return value.hex
    return value


async def _active_organization_ids(session: AsyncSession) -> list[uuid.UUID]:
    # organizations is the tenant root, defined in soa_api (not soa_db); a
    # raw, portable query avoids a worker -> api layering dependency, the
    # same technique soa_worker.run_config uses for policy_versions.
    rows = (
        await session.execute(text("SELECT id FROM organizations WHERE status = 'active'"))
    ).all()
    return [row[0] if isinstance(row[0], uuid.UUID) else uuid.UUID(str(row[0])) for row in rows]


async def _published_retention_days(
    session: AsyncSession, organization_id: uuid.UUID
) -> int | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT definition FROM policy_versions "
                    "WHERE organization_id = :organization_id "
                    "AND policy_type = 'retention' AND state = 'published'"
                ),
                {"organization_id": _database_identifier(session, organization_id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    definition = row["definition"]
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except json.JSONDecodeError:
            return None
    if not isinstance(definition, dict):
        return None
    days = definition.get("document_days")
    return days if isinstance(days, int) and days >= 1 else None


class RetentionCoordinator:
    """Boundedly sweep settled documents whose retention window elapsed."""

    def __init__(self, db: DatabaseSessions) -> None:
        self._db = db

    async def reconcile(self, *, limit_per_org: int = 20) -> int:
        async with self._db.session_scope() as session:
            organization_ids = await _active_organization_ids(session)
        total = 0
        for organization_id in organization_ids:
            total += await self._reconcile_organization(organization_id, limit=limit_per_org)
        return total

    async def _reconcile_organization(self, organization_id: uuid.UUID, *, limit: int) -> int:
        context = OrganizationContext(organization_id=organization_id)
        created = 0
        async with self._db.session_scope() as session:
            await bind_tenant(session, organization_id)
            days = await _published_retention_days(session, organization_id)
            if days is None:
                return 0  # no published retention policy: nothing to enforce
            policy = RetentionPolicy(base_days=days)
            now = utcnow()

            already_requested = select(DeletionRequest.document_id).where(
                DeletionRequest.organization_id == organization_id
            )
            candidates = (
                (
                    await session.execute(
                        select(Document)
                        .where(
                            Document.organization_id == organization_id,
                            Document.state.in_(DELETION_SETTLED_STATES),
                            Document.id.notin_(already_requested),
                        )
                        .order_by(Document.updated_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

            for document in candidates:
                artifact_classes = (
                    (
                        await session.execute(
                            select(Artifact.retention_class).where(
                                Artifact.document_id == document.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if any(value != RetentionClass.STANDARD.value for value in artifact_classes):
                    # Extended retention / legal hold artifact classes are a
                    # human decision this sweep never overrides.
                    continue
                hold = await LegalHoldRepository(session, context).active_for_document(document.id)
                decision = evaluate_retention(
                    settled_at=document.updated_at,
                    retention_class=RetentionClass.STANDARD,
                    policy=policy,
                    now=now,
                    legal_hold_active=hold is not None,
                )
                if not decision.eligible:
                    continue
                try:
                    await request_document_deletion(
                        session,
                        context,
                        document_id=document.id,
                        reason=f"Automatic retention sweep: {decision.reasons[-1]}",
                        actor_id=RETENTION_SCHEDULER_ACTOR,
                    )
                    created += 1
                except DeletionLifecycleError as error:
                    # A concurrent change (e.g. a hold placed, or the
                    # document already deleted) between our read and this
                    # call — safe to skip; the next sweep re-evaluates it.
                    logger.warning(
                        "retention sweep skipped a document",
                        extra={"document_id": str(document.id), "reason": str(error)},
                    )
        return created


__all__ = ["RETENTION_SCHEDULER_ACTOR", "RetentionCoordinator"]
