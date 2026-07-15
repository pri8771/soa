"""Cost and usage ledger (ANA-003).

An APPEND-ONLY ledger of what the platform consumed and what it cost,
by tenant / stream / document / run / page count / provider / model /
cost category. Two honesty separations run through the design:

- **billed units vs estimated cost** — ``billed_unit`` and
  ``billed_quantity`` record what the provider actually meters (pages,
  tokens, calls): FACTS. ``estimated_cost_cents`` is OUR price
  estimate at recording time. The two are never merged, so a provider
  invoice can be reconciled against billed units without trusting the
  estimate.
- **immutable adjustments** — ledger rows never change (a flush guard
  refuses updates). Reconciliation against a real invoice happens by
  APPENDING an adjustment entry that references the entry it corrects
  and carries a signed delta and a written reason; the audit trail
  records who adjusted what and why. ``reconciled_cents`` is always
  estimate + adjustments, recomputable from the rows alone.

Idempotency: an optional ``source_reference`` (e.g. the stage-run id
plus stage name) is unique per organization — retried pipeline stages
cannot double-bill.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, String, UniqueConstraint, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID

COST_CATEGORIES = frozenset(
    {
        "rendering",
        "ocr",
        "classification",
        "extraction",
        "matching",
        "storage",
        "export",
        "other",
    }
)

ENTRY_TYPES = ("usage", "adjustment")


class UsageLedgerError(ValueError):
    pass


class UsageEntryImmutableError(Exception):
    def __init__(self, entry_id: object) -> None:
        super().__init__(
            f"usage ledger entry {entry_id} is immutable — corrections are new "
            "adjustment entries, never edits"
        )


class UsageEntry(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "usage_ledger_entries"

    entry_type: Mapped[str] = mapped_column(String(20), nullable=False, default="usage")
    stream_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    page_count: Mapped[int] = mapped_column(nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_category: Mapped[str] = mapped_column(String(30), nullable=False)
    #: What the provider meters (pages/tokens/calls) — facts for
    #: reconciliation, distinct from our estimate.
    billed_unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    billed_quantity: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    #: OUR price estimate at recording time.
    estimated_cost_cents: Mapped[int] = mapped_column(nullable=False, default=0)
    #: Signed delta applied by an adjustment entry (0 on usage entries).
    adjustment_cents: Mapped[int] = mapped_column(nullable=False, default=0)
    adjusts_entry_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Idempotency key (unique per organization when present).
    source_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("organization_id", "source_reference"),)


@event.listens_for(Session, "before_flush")
def _refuse_usage_entry_mutation(
    session: Session, _flush_context: object, _instances: object
) -> None:
    for instance in session.dirty:
        if isinstance(instance, UsageEntry) and session.is_modified(instance):
            raise UsageEntryImmutableError(instance.id)


class UsageEntryRepository(ScopedRepository[UsageEntry]):
    model = UsageEntry

    async def get_by_source_reference(self, source_reference: str) -> UsageEntry | None:
        stmt = self._scoped_select().where(UsageEntry.source_reference == source_reference)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_adjustments_for(self, entry_id: uuid.UUID) -> list[UsageEntry]:
        stmt = (
            self._scoped_select()
            .where(UsageEntry.adjusts_entry_id == entry_id)
            .order_by(UsageEntry.created_at, UsageEntry.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def record_usage(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    provider: str,
    cost_category: str,
    estimated_cost_cents: int,
    actor_id: str,
    provider_model: str | None = None,
    billed_unit: str | None = None,
    billed_quantity: int | None = None,
    stream_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    page_count: int = 0,
    source_reference: str | None = None,
    reason: str | None = None,
) -> UsageEntry:
    """Append one usage entry. Idempotent on ``source_reference``: a
    retried stage returns the existing entry instead of double-billing."""
    if cost_category not in COST_CATEGORIES:
        raise UsageLedgerError(f"cost_category must be one of {sorted(COST_CATEGORIES)}")
    if estimated_cost_cents < 0:
        raise UsageLedgerError("estimated_cost_cents cannot be negative — credits are adjustments")
    if page_count < 0:
        raise UsageLedgerError("page_count cannot be negative")
    if (billed_unit is None) != (billed_quantity is None):
        raise UsageLedgerError(
            "billed_unit and billed_quantity travel together — a quantity without "
            "its unit (or vice versa) cannot be reconciled"
        )
    if billed_quantity is not None and billed_quantity < 0:
        raise UsageLedgerError("billed_quantity cannot be negative")
    if reason is not None and (not reason.strip() or len(reason) > 500):
        raise UsageLedgerError("reason must be 1..500 characters when supplied")

    repo = UsageEntryRepository(session, context)
    if source_reference is not None:
        existing = await repo.get_by_source_reference(source_reference)
        if existing is not None:
            return existing
    entry = repo.add(
        UsageEntry(
            entry_type="usage",
            stream_id=stream_id,
            document_id=document_id,
            run_id=run_id,
            page_count=page_count,
            provider=provider,
            provider_model=provider_model,
            cost_category=cost_category,
            billed_unit=billed_unit,
            billed_quantity=billed_quantity,
            estimated_cost_cents=estimated_cost_cents,
            source_reference=source_reference,
            reason=reason,
            created_by=actor_id,
        )
    )
    await session.flush()
    return entry


async def record_adjustment(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    entry: UsageEntry,
    delta_cents: int,
    reason: str,
    actor_id: str,
) -> UsageEntry:
    """Append a reconciliation adjustment against one usage entry: a
    signed delta with a WRITTEN reason, audited. The original row never
    changes."""
    if not reason.strip():
        raise UsageLedgerError("an adjustment needs a written reason")
    if entry.entry_type != "usage":
        raise UsageLedgerError("adjustments apply to usage entries, not to other adjustments")
    adjustment = UsageEntryRepository(session, context).add(
        UsageEntry(
            entry_type="adjustment",
            stream_id=entry.stream_id,
            document_id=entry.document_id,
            run_id=entry.run_id,
            provider=entry.provider,
            provider_model=entry.provider_model,
            cost_category=entry.cost_category,
            # Same billing context as the entry it corrects, so the
            # summary reconciles them within one group.
            billed_unit=entry.billed_unit,
            adjustment_cents=delta_cents,
            adjusts_entry_id=entry.id,
            reason=reason,
            created_by=actor_id,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="usage.adjusted",
        target_type="usage_entry",
        target_id=str(entry.id),
        organization_id=context.organization_id,
        summary={
            "adjustment_id": str(adjustment.id),
            "delta_cents": delta_cents,
            "reason": reason,
        },
    )
    return adjustment


async def usage_summary(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    """Aggregated usage for the window (half-open, UTC), grouped by
    stream / provider / model / cost category / billed unit. Estimated
    cost, adjustments, and the reconciled total stay separate columns —
    and billed quantities are summed only within their own unit."""
    dims = (
        UsageEntry.stream_id,
        UsageEntry.provider,
        UsageEntry.provider_model,
        UsageEntry.cost_category,
        UsageEntry.billed_unit,
    )
    rows = await session.execute(
        select(
            *dims,
            func.sum(UsageEntry.estimated_cost_cents).label("estimated_cents"),
            func.sum(UsageEntry.adjustment_cents).label("adjustment_cents"),
            func.sum(UsageEntry.billed_quantity).label("billed_quantity"),
            func.sum(UsageEntry.page_count).label("pages"),
            func.count().label("entries"),
        )
        .where(
            UsageEntry.organization_id == context.organization_id,
            UsageEntry.created_at >= since,
            UsageEntry.created_at < until,
        )
        .group_by(*dims)
        .order_by(UsageEntry.provider, UsageEntry.cost_category)
    )
    groups = []
    total_estimated = 0
    total_adjustment = 0
    for row in rows:
        estimated = int(row.estimated_cents or 0)
        adjustment = int(row.adjustment_cents or 0)
        total_estimated += estimated
        total_adjustment += adjustment
        groups.append(
            {
                "stream_id": str(row.stream_id) if row.stream_id else None,
                "provider": row.provider,
                "provider_model": row.provider_model,
                "cost_category": row.cost_category,
                "billed_unit": row.billed_unit,
                "billed_quantity": int(row.billed_quantity) if row.billed_quantity else None,
                "pages": int(row.pages or 0),
                "entries": row.entries,
                "estimated_cents": estimated,
                "adjustment_cents": adjustment,
                "reconciled_cents": estimated + adjustment,
            }
        )
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat(), "timezone": "UTC"},
        "groups": groups,
        "totals": {
            "estimated_cents": total_estimated,
            "adjustment_cents": total_adjustment,
            "reconciled_cents": total_estimated + total_adjustment,
        },
        "semantics": {
            "estimated_cents": "our price estimate at recording time",
            "billed_quantity": "provider-metered units (facts), summed per billed_unit only",
            "reconciled_cents": "estimated + appended adjustments; rows are immutable",
        },
    }


__all__ = [
    "COST_CATEGORIES",
    "ENTRY_TYPES",
    "UsageEntry",
    "UsageEntryImmutableError",
    "UsageEntryRepository",
    "UsageLedgerError",
    "record_adjustment",
    "record_usage",
    "usage_summary",
]
