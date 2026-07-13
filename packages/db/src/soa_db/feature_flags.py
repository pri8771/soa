"""Feature flags and quota policy (ANA-009).

Typed, owned, expiring toggles — a flag nobody owns or reviews is how
dead switches accumulate:

- **typed** — a row is either a boolean ``flag`` or a numeric ``quota``
  (limit + unit); the writer refuses mixed or missing shapes;
- **scoped** — organization-wide or per-stream; a stream row overrides
  the organization row on resolution;
- **owned and reviewed** — ``owner`` and ``review_by`` are REQUIRED;
  past ``review_by`` a row is INERT (resolved as absent, with the
  expiry stated) until someone re-reviews it — stale flags fail safe;
- **not a security switch** — keys under ``security.`` are refused by
  this API entirely. Security controls change through code review and
  the SEC epic's own controls, never through a runtime toggle;
- **audited** — every create/update writes an audit event with the
  before/after summary.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.audit import ActorType, record_audit_event
from soa_db.base import Base
from soa_db.mixins import TimestampMixin, UuidPrimaryKeyMixin
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow

FLAG_KINDS = ("flag", "quota")
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{2,99}$")
#: Keys the runtime toggle API refuses outright.
PROTECTED_PREFIXES = ("security.",)


class FeatureFlagError(ValueError):
    pass


class FeatureFlag(UuidPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    #: NULL = organization-wide; set = a per-stream override.
    stream_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    enabled: Mapped[bool | None] = mapped_column(nullable=True)
    limit_value: Mapped[int | None] = mapped_column(nullable=True)
    limit_unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    review_by: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("organization_id", "key", "stream_id"),)


class FeatureFlagRepository(ScopedRepository[FeatureFlag]):
    model = FeatureFlag

    async def get_by_key(self, key: str, stream_id: uuid.UUID | None) -> FeatureFlag | None:
        stmt = self._scoped_select().where(
            FeatureFlag.key == key, FeatureFlag.stream_id == stream_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[FeatureFlag]:
        stmt = self._scoped_select().order_by(FeatureFlag.key, FeatureFlag.stream_id)
        return list((await self._session.execute(stmt)).scalars().all())


def _validate_common(key: str, owner: str, review_by: datetime, description: str) -> None:
    if not _KEY_PATTERN.fullmatch(key):
        raise FeatureFlagError(
            "keys are lowercase dotted identifiers, 3-100 chars (e.g. 'quota.monthly_cost_cents')"
        )
    if any(key.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        raise FeatureFlagError(
            f"{key!r} names a security control — security controls are not runtime "
            "toggles and cannot be created or disabled through the flag API"
        )
    if not owner.strip():
        raise FeatureFlagError("every flag needs a named owner")
    if not description.strip():
        raise FeatureFlagError("every flag needs a description")
    if review_by <= utcnow():
        raise FeatureFlagError("review_by must be in the future — stale flags are inert")


async def set_flag(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    key: str,
    enabled: bool | None = None,
    limit_value: int | None = None,
    limit_unit: str | None = None,
    description: str,
    owner: str,
    review_by: datetime,
    actor_id: str,
    stream_id: uuid.UUID | None = None,
) -> FeatureFlag:
    """Create or update a flag/quota row, audited. Exactly one shape:
    ``enabled`` for a flag, ``limit_value`` + ``limit_unit`` for a quota."""
    _validate_common(key, owner, review_by, description)
    is_flag = enabled is not None
    is_quota = limit_value is not None or limit_unit is not None
    if is_flag == is_quota:
        raise FeatureFlagError(
            "a row is either a flag (enabled) or a quota (limit_value + limit_unit), not both"
        )
    if is_quota and (limit_value is None or limit_value < 0 or not (limit_unit or "").strip()):
        raise FeatureFlagError("a quota needs a non-negative limit_value and a limit_unit")

    repo = FeatureFlagRepository(session, context)
    existing = await repo.get_by_key(key, stream_id)
    before: dict[str, Any] | None = None
    if existing is None:
        row = repo.add(
            FeatureFlag(
                key=key,
                kind="flag" if is_flag else "quota",
                stream_id=stream_id,
                enabled=enabled,
                limit_value=limit_value,
                limit_unit=limit_unit,
                description=description,
                owner=owner,
                review_by=review_by,
                created_by=actor_id,
            )
        )
    else:
        if existing.kind != ("flag" if is_flag else "quota"):
            raise FeatureFlagError(
                f"{key!r} already exists as a {existing.kind} — kinds never change in place"
            )
        before = {
            "enabled": existing.enabled,
            "limit_value": existing.limit_value,
            "limit_unit": existing.limit_unit,
            "owner": existing.owner,
            "review_by": existing.review_by.isoformat(),
        }
        existing.enabled = enabled
        existing.limit_value = limit_value
        existing.limit_unit = limit_unit
        existing.description = description
        existing.owner = owner
        existing.review_by = review_by
        row = existing
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="feature_flag.set",
        target_type="feature_flag",
        target_id=str(row.id),
        organization_id=context.organization_id,
        summary={
            "key": key,
            "kind": row.kind,
            "stream_id": str(stream_id) if stream_id else None,
            "before": before,
            "after": {
                "enabled": row.enabled,
                "limit_value": row.limit_value,
                "limit_unit": row.limit_unit,
                "owner": row.owner,
                "review_by": row.review_by.isoformat(),
            },
        },
    )
    return row


@dataclass(frozen=True)
class ResolvedFlag:
    key: str
    kind: str
    enabled: bool | None
    limit_value: int | None
    limit_unit: str | None
    #: Which row answered: "stream" | "organization" | "absent" | "expired".
    source: str
    notes: tuple[str, ...] = ()


async def resolve_flag(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    key: str,
    stream_id: uuid.UUID | None = None,
) -> ResolvedFlag:
    """Stream row overrides organization row; rows past their review
    date are INERT (resolved as absent, stated in the notes)."""
    repo = FeatureFlagRepository(session, context)
    now = utcnow()
    candidates: list[tuple[str, FeatureFlag | None]] = []
    if stream_id is not None:
        candidates.append(("stream", await repo.get_by_key(key, stream_id)))
    candidates.append(("organization", await repo.get_by_key(key, None)))
    notes: list[str] = []
    for source, row in candidates:
        if row is None:
            continue
        if row.review_by <= now:
            notes.append(
                f"the {source} row for {key!r} passed its review date "
                f"({row.review_by.date().isoformat()}) and is inert until re-reviewed"
            )
            continue
        return ResolvedFlag(
            key=key,
            kind=row.kind,
            enabled=row.enabled,
            limit_value=row.limit_value,
            limit_unit=row.limit_unit,
            source=source,
            notes=tuple(notes),
        )
    return ResolvedFlag(
        key=key,
        kind="absent",
        enabled=None,
        limit_value=None,
        limit_unit=None,
        source="expired" if notes else "absent",
        notes=tuple(notes),
    )


__all__ = [
    "FLAG_KINDS",
    "PROTECTED_PREFIXES",
    "FeatureFlag",
    "FeatureFlagError",
    "FeatureFlagRepository",
    "ResolvedFlag",
    "resolve_flag",
    "set_flag",
]
