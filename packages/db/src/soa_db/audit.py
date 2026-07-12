"""Append-oriented audit events (ADR-022, AGENTS.md §6/§7).

Every consequential automated or human action records who, what, when,
where, and why-context. Audit rows are immutable: session-level guards
reject any UPDATE or DELETE of an ``AuditEvent`` at flush time, so no
application code path — repository or ad-hoc — can silently rewrite
history. Change summaries pass through key-based redaction before storage.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Index, String, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_config.logging import get_correlation_id, redact_mapping
from soa_db.base import Base
from soa_db.mixins import UuidPrimaryKeyMixin
from soa_db.types import GUID, UTCDateTime, utcnow

PORTABLE_JSON = JSON().with_variant(JSONB(), "postgresql")


class AuditImmutabilityError(Exception):
    """Raised when application code attempts to modify or delete audit rows."""


class ActorType(StrEnum):
    USER = "user"
    SERVICE = "service"
    SYSTEM = "system"


class AuditEvent(UuidPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"

    # Nullable only for platform-level actions; tenant actions must set it.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    target_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)

    __table_args__ = (
        Index("ix_audit_events_org_occurred", "organization_id", "occurred_at"),
        Index("ix_audit_events_target", "target_type", "target_id"),
    )


@event.listens_for(Session, "before_flush")
def _reject_audit_mutations(session: Session, flush_context: object, instances: object) -> None:
    for dirty in session.dirty:
        if isinstance(dirty, AuditEvent) and session.is_modified(dirty):
            raise AuditImmutabilityError("audit events are append-only and cannot be updated")
    for deleted in session.deleted:
        if isinstance(deleted, AuditEvent):
            raise AuditImmutabilityError("audit events are append-only and cannot be deleted")


async def record_audit_event(
    session: AsyncSession,
    *,
    actor_type: ActorType,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    organization_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
    summary: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit event in the caller's transaction.

    The correlation ID defaults to the ambient request/job correlation
    context; summaries are redacted key-by-key before storage.
    """
    audit = AuditEvent(
        organization_id=organization_id,
        actor_type=actor_type.value,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id if correlation_id is not None else get_correlation_id(),
        summary=redact_mapping(summary) if summary is not None else None,
    )
    session.add(audit)
    return audit
