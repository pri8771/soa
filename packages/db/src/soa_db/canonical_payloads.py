"""Canonical payload persistence (CAN-003).

The canonical order built at approval time is stored EXACTLY as
approved: one row per (run, schema version), append-only and immutable
— a flush guard refuses updates, and re-recording is idempotent only
when the payload is byte-identical (same content hash). Export and the
payload viewer read from here; nothing downstream ever re-derives the
order from raw extractions.
"""

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import String, UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID


class CanonicalPayloadImmutableError(Exception):
    def __init__(self, run_id: object, schema_version: str) -> None:
        super().__init__(
            f"a canonical payload for run {run_id} at schema {schema_version} already "
            "exists with different content — approved payloads are immutable; a new "
            "payload requires a new run or a new schema version"
        )


def payload_sha256(payload: dict[str, Any]) -> str:
    """Content hash over the canonical JSON serialization (sorted keys,
    no whitespace) so equality is about content, not formatting."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CanonicalPayload(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "canonical_payloads"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "schema_version"),)


@event.listens_for(Session, "before_flush")
def _refuse_canonical_payload_mutation(
    session: Session, _flush_context: object, _instances: object
) -> None:
    for obj in session.dirty:
        if isinstance(obj, CanonicalPayload) and session.is_modified(obj):
            raise CanonicalPayloadImmutableError(obj.run_id, obj.schema_version)
    for obj in session.deleted:
        if isinstance(obj, CanonicalPayload):
            raise CanonicalPayloadImmutableError(obj.run_id, obj.schema_version)


class CanonicalPayloadRepository(ScopedRepository[CanonicalPayload]):
    model = CanonicalPayload

    async def get_for_run(self, run_id: uuid.UUID, schema_version: str) -> CanonicalPayload | None:
        stmt = self._scoped_select().where(
            CanonicalPayload.run_id == run_id,
            CanonicalPayload.schema_version == schema_version,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def latest_for_document(self, document_id: uuid.UUID) -> CanonicalPayload | None:
        stmt = (
            self._scoped_select()
            .where(CanonicalPayload.document_id == document_id)
            .order_by(CanonicalPayload.created_at.desc(), CanonicalPayload.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def record_canonical_payload(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    schema_version: str,
    payload: dict[str, Any],
    actor_id: str,
) -> CanonicalPayload:
    """Persist the approved canonical payload. Idempotent for identical
    content; DIFFERENT content for the same (run, schema version) is an
    immutability violation, not an overwrite."""
    repo = CanonicalPayloadRepository(session, context)
    digest = payload_sha256(payload)
    existing = await repo.get_for_run(run_id, schema_version)
    if existing is not None:
        if existing.sha256 == digest:
            return existing
        raise CanonicalPayloadImmutableError(run_id, schema_version)
    row = repo.add(
        CanonicalPayload(
            document_id=document_id,
            run_id=run_id,
            task_id=task_id,
            schema_version=schema_version,
            payload=payload,
            sha256=digest,
            created_by=actor_id,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="canonical_payload.recorded",
        target_type="document",
        target_id=str(document_id),
        organization_id=context.organization_id,
        summary={
            "run_id": str(run_id),
            "schema_version": schema_version,
            "sha256": digest,
            "line_items": len(payload.get("line_items", [])),
        },
    )
    return row


__all__ = [
    "CanonicalPayload",
    "CanonicalPayloadImmutableError",
    "CanonicalPayloadRepository",
    "payload_sha256",
    "record_canonical_payload",
]
