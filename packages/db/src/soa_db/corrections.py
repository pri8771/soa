"""Field corrections (REV-009).

Corrections are APPEND-ONLY: the original extraction (PRC-007) is never
modified — every human change is a new row recording who changed what,
from which value to which value, why, and (optionally) which evidence
they relied on. The effective value of a field is the LATEST correction
when one exists, otherwise the extraction's canonical value; nothing is
ever silently overwritten because nothing is overwritten at all.

Rows carry the review-task version they were written against so the API
layer can refuse stale writes (optimistic concurrency) and the audit
trail can reconstruct the exact sequence of review edits.
"""

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, Text, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID


class FieldCorrection(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "field_corrections"

    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String(255), nullable=False)
    row_index: Mapped[int | None] = mapped_column(nullable=True)
    #: The effective value BEFORE this correction (raw form), for display
    #: and undo; the immutable extraction row remains the true original.
    previous_raw_value: Mapped[str | None] = mapped_column(Text(), nullable=True)
    corrected_raw_value: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Canonical form of the corrected value (PRC-008); null when the
    #: corrected input could not be normalized (kept with its error).
    corrected_normalized_value: Mapped[Any | None] = mapped_column(PORTABLE_JSON, nullable=True)
    normalization_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: The evidence span the reviewer relied on, if they picked one.
    evidence_selection: Mapped[dict[str, Any] | None] = mapped_column(PORTABLE_JSON, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Review-task version this correction was written against.
    task_version: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (Index("ix_field_corrections_run_field", "run_id", "field_key"),)


@event.listens_for(Session, "before_flush")
def _refuse_correction_mutation(session: Session, _ctx: object, _instances: object) -> None:
    for entity in session.dirty:
        if isinstance(entity, FieldCorrection) and session.is_modified(entity):
            raise ValueError(
                f"field correction {entity.id} is append-only — record a new correction"
            )


class CorrectionState(StrEnum):
    """Placeholder for future soft-invalidations; corrections currently
    never change state (they are superseded by newer rows)."""

    RECORDED = "recorded"


class FieldCorrectionRepository(ScopedRepository[FieldCorrection]):
    model = FieldCorrection

    async def list_for_run(self, run_id: uuid.UUID) -> list[FieldCorrection]:
        stmt = (
            self._scoped_select()
            .where(FieldCorrection.run_id == run_id)
            .order_by(FieldCorrection.created_at, FieldCorrection.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


def latest_corrections(
    corrections: list[FieldCorrection],
) -> dict[tuple[str, int | None], FieldCorrection]:
    """Last write per (field, row) — list_for_run's ordering makes the
    final assignment the newest."""
    latest: dict[tuple[str, int | None], FieldCorrection] = {}
    for correction in corrections:
        latest[(correction.field_key, correction.row_index)] = correction
    return latest


async def record_correction(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    field_key: str,
    row_index: int | None,
    previous_raw_value: str | None,
    corrected_raw_value: str | None,
    corrected_normalized_value: Any | None,
    normalization_error: str | None,
    reason: str | None,
    evidence_selection: dict[str, Any] | None,
    corrected_by: str,
    task_version: int,
) -> FieldCorrection:
    correction = FieldCorrectionRepository(session, context).add(
        FieldCorrection(
            document_id=document_id,
            run_id=run_id,
            task_id=task_id,
            field_key=field_key,
            row_index=row_index,
            previous_raw_value=previous_raw_value,
            corrected_raw_value=corrected_raw_value,
            corrected_normalized_value=corrected_normalized_value,
            normalization_error=normalization_error,
            reason=reason,
            evidence_selection=evidence_selection,
            corrected_by=corrected_by,
            task_version=task_version,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=corrected_by,
        action="review.field_corrected",
        target_type="review_task",
        target_id=str(task_id),
        organization_id=context.organization_id,
        summary={
            "field_key": field_key,
            "row_index": row_index,
            "reason": reason,
            "task_version": task_version,
        },
    )
    return correction
