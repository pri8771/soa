"""Global, privacy-safe abuse-control state.

Rate limits can apply before an organization is known (for example the
identity-bootstrap client address), and authenticated identities may span
organizations. These two tables are therefore deliberately global and do not
carry ``organization_id`` or tenant RLS. They contain only:

* an HMAC-SHA256 identity digest scoped to one operation;
* bounded accepted-event timestamps for the active window; and
* aggregate per-operation counters.

Raw tenant, user, credential, and network identities are never persisted.
Business data and billing quotas do not belong in these tables.
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from soa_db.base import Base
from soa_db.outbox import PORTABLE_JSON
from soa_db.types import UTCDateTime


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"

    operation: Mapped[str] = mapped_column(String(100), primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    events: Mapped[list[float]] = mapped_column(PORTABLE_JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint("length(operation) BETWEEN 1 AND 100", name="operation_length"),
        CheckConstraint("length(identity_hash) = 64", name="identity_hash_length"),
        Index("ix_rate_limit_buckets_expires_at", "expires_at"),
    )


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"

    operation: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Digest-derived shards preserve exact aggregates without forcing all
    # identities for one operation through a single hot counter row.
    shard: Mapped[int] = mapped_column(SmallInteger(), primary_key=True)
    allowed: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    denied: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint("length(operation) BETWEEN 1 AND 100", name="operation_length"),
        CheckConstraint("shard BETWEEN 0 AND 255", name="shard_range"),
        CheckConstraint("allowed >= 0", name="allowed_nonnegative"),
        CheckConstraint("denied >= 0", name="denied_nonnegative"),
    )


__all__ = ["RateLimitBucket", "RateLimitCounter"]
