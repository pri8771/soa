"""Rate limiting and cross-replica abuse controls (SEC-003).

Development and tests use an in-memory sliding window. Staging and production
use :class:`DatabaseSlidingWindowRateLimiter`, which makes each decision in a
short dedicated database transaction so multiple API replicas share one
atomic budget. The durable form persists only a keyed HMAC identity digest and
aggregate operation counters—never raw tenant, user, credential, or IP data.

Limits are abuse controls, not billing quotas. A backend failure fails closed
with HTTP 503 and ``Retry-After``; it never falls back to a replica-local budget.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import HTTPException, status
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db import DatabaseSessions
from soa_db.rate_limits import RateLimitBucket, RateLimitCounter

logger = logging.getLogger(__name__)

_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
MAX_EVENTS_PER_BUCKET = 10_000


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    operation: str
    limit: int
    remaining: int
    retry_after_seconds: int

    def headers(self) -> dict[str, str]:
        values = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            values["Retry-After"] = str(self.retry_after_seconds)
        return values


class RateLimitBackendUnavailable(RuntimeError):
    """The durable limiter could not make an atomic decision."""


class RateLimiter(Protocol):
    async def check(
        self,
        operation: str,
        identity: str,
        limit: int,
        *,
        now: float | datetime | None = None,
    ) -> RateLimitDecision: ...

    async def enforce(
        self,
        operation: str,
        identity: str,
        limit: int,
        *,
        now: float | datetime | None = None,
    ) -> RateLimitDecision: ...

    async def snapshot(self) -> dict[str, dict[str, int]]: ...

    async def ready(self) -> bool: ...


def _validate_request(operation: str, identity: str, limit: int) -> None:
    if not _OPERATION_PATTERN.fullmatch(operation):
        raise ValueError("rate-limit operation must be a bounded lowercase identifier")
    if not identity or len(identity) > 1000:
        raise ValueError("rate-limit identity must contain 1-1000 characters")
    if not 1 <= limit <= MAX_EVENTS_PER_BUCKET:
        raise ValueError(f"rate-limit limit must be between 1 and {MAX_EVENTS_PER_BUCKET}")


def _raise_denied(decision: RateLimitDecision) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"Rate limit exceeded for {decision.operation} — retry in "
            f"{decision.retry_after_seconds}s."
        ),
        headers=decision.headers(),
    )


@dataclass
class SlidingWindowRateLimiter:
    """Replica-local sliding windows for development and isolated tests."""

    window_seconds: float = 60.0
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _counters: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"allowed": 0, "denied": 0})
    )

    async def check(
        self,
        operation: str,
        identity: str,
        limit: int,
        *,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        _validate_request(operation, identity, limit)
        if isinstance(now, datetime):
            current = now.timestamp()
        else:
            current = now if now is not None else time.monotonic()
        key = f"{operation}:{identity}"
        events = self._events[key]
        while events and current - events[0] > self.window_seconds:
            events.popleft()
        if len(events) >= limit:
            self._counters[operation]["denied"] += 1
            retry_after = max(1, math.ceil(self.window_seconds - (current - events[0])))
            logger.warning(
                "rate limit denied",
                extra={
                    "operation": operation,
                    "identity_hash": hashlib.sha256(identity.encode()).hexdigest()[:12],
                    "limit": limit,
                    "backend": "memory",
                },
            )
            return RateLimitDecision(False, operation, limit, 0, retry_after)
        events.append(current)
        self._counters[operation]["allowed"] += 1
        return RateLimitDecision(True, operation, limit, limit - len(events), 0)

    async def enforce(
        self,
        operation: str,
        identity: str,
        limit: int,
        *,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        decision = await self.check(operation, identity, limit, now=now)
        if not decision.allowed:
            _raise_denied(decision)
        return decision

    async def snapshot(self) -> dict[str, dict[str, int]]:
        return {operation: dict(counts) for operation, counts in sorted(self._counters.items())}

    async def ready(self) -> bool:
        return True


@dataclass
class DatabaseSlidingWindowRateLimiter:
    """Atomic database-backed sliding windows shared by every API replica."""

    database: DatabaseSessions
    hash_secret: SecretStr
    window_seconds: float = 60.0
    cleanup_batch_size: int = 100

    def __post_init__(self) -> None:
        secret = self.hash_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("database rate-limit hash secret must be at least 32 characters")
        if not 1 <= self.window_seconds <= 3600:
            raise ValueError("database rate-limit window must be between 1 and 3600 seconds")
        if not 1 <= self.cleanup_batch_size <= 1000:
            raise ValueError("database rate-limit cleanup batch must be between 1 and 1000")

    def _identity_hash(self, operation: str, identity: str) -> str:
        secret = self.hash_secret.get_secret_value()
        message = f"soa-rate-limit-v1\0{operation}\0{identity}".encode()
        return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    @staticmethod
    def _normalize_now(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _decision_time(
        self, session: AsyncSession, supplied: float | datetime | None
    ) -> datetime:
        if isinstance(supplied, datetime):
            return self._normalize_now(supplied)
        if isinstance(supplied, float | int):
            return datetime.fromtimestamp(supplied, tz=UTC)
        database_now = await session.scalar(select(func.current_timestamp()))
        if not isinstance(database_now, datetime):
            raise RuntimeError("database did not return a timestamp for rate limiting")
        return self._normalize_now(database_now)

    async def _delete_expired(self, session: AsyncSession, now: datetime) -> None:
        expired_keys = list(
            (
                await session.execute(
                    select(RateLimitBucket.operation, RateLimitBucket.identity_hash)
                    .where(RateLimitBucket.expires_at <= now)
                    .order_by(RateLimitBucket.expires_at)
                    .limit(self.cleanup_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if not expired_keys:
            return
        await session.execute(
            delete(RateLimitBucket).where(
                tuple_(RateLimitBucket.operation, RateLimitBucket.identity_hash).in_(expired_keys)
            )
        )

    @staticmethod
    async def _ensure_bucket(
        session: AsyncSession,
        *,
        operation: str,
        identity_hash: str,
        now: datetime,
    ) -> RateLimitBucket:
        values = {
            "operation": operation,
            "identity_hash": identity_hash,
            "events": [],
            "updated_at": now,
            "expires_at": now,
        }
        dialect = session.get_bind().dialect.name
        statement: Any
        if dialect == "postgresql":
            statement = postgresql_insert(RateLimitBucket).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(RateLimitBucket).values(**values)
        else:
            raise RuntimeError(f"unsupported durable rate-limit database dialect {dialect!r}")
        await session.execute(statement.on_conflict_do_nothing())
        bucket = await session.scalar(
            select(RateLimitBucket)
            .where(
                RateLimitBucket.operation == operation,
                RateLimitBucket.identity_hash == identity_hash,
            )
            .with_for_update()
        )
        if bucket is None:
            raise RuntimeError("rate-limit bucket disappeared during atomic decision")
        return bucket

    @staticmethod
    async def _increment_counter(
        session: AsyncSession,
        *,
        operation: str,
        identity_hash: str,
        allowed: bool,
        now: datetime,
    ) -> None:
        allowed_increment = 1 if allowed else 0
        denied_increment = 0 if allowed else 1
        values = {
            "operation": operation,
            "shard": int(identity_hash[:2], 16),
            "allowed": allowed_increment,
            "denied": denied_increment,
            "updated_at": now,
        }
        dialect = session.get_bind().dialect.name
        statement: Any
        if dialect == "postgresql":
            statement = postgresql_insert(RateLimitCounter).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(RateLimitCounter).values(**values)
        else:
            raise RuntimeError(f"unsupported durable rate-limit database dialect {dialect!r}")
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[RateLimitCounter.operation, RateLimitCounter.shard],
                set_={
                    "allowed": RateLimitCounter.allowed + allowed_increment,
                    "denied": RateLimitCounter.denied + denied_increment,
                    "updated_at": now,
                },
            )
        )

    async def check(
        self,
        operation: str,
        identity: str,
        limit: int,
        *,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        _validate_request(operation, identity, limit)
        identity_hash = self._identity_hash(operation, identity)
        try:
            async with self.database.session_scope() as session:
                # PostgreSQL row locks serialize a single digest. SQLite's
                # FOR UPDATE is a no-op, so tests/local validation take the
                # write lock before reading to exercise equivalent atomicity.
                if session.get_bind().dialect.name == "sqlite":
                    await session.execute(text("BEGIN IMMEDIATE"))
                current = await self._decision_time(session, now)
                await self._delete_expired(session, current)
                bucket = await self._ensure_bucket(
                    session,
                    operation=operation,
                    identity_hash=identity_hash,
                    now=current,
                )
                current_epoch = current.timestamp()
                cutoff = current_epoch - self.window_seconds
                events = sorted(float(event) for event in bucket.events if float(event) >= cutoff)
                allowed = len(events) < limit
                if allowed:
                    events.append(current_epoch)
                    retry_after = 0
                    remaining = limit - len(events)
                else:
                    retry_after = max(
                        1,
                        math.ceil(events[0] + self.window_seconds - current_epoch),
                    )
                    remaining = 0
                bucket.events = events
                bucket.updated_at = current
                bucket.expires_at = datetime.fromtimestamp(events[-1] + self.window_seconds, tz=UTC)
                await self._increment_counter(
                    session,
                    operation=operation,
                    identity_hash=identity_hash,
                    allowed=allowed,
                    now=current,
                )
        except Exception as exc:
            logger.error(
                "durable rate-limit decision failed closed",
                extra={
                    "operation": operation,
                    "identity_hash": identity_hash[:12],
                    "backend": "database",
                },
                exc_info=True,
            )
            raise RateLimitBackendUnavailable("durable rate-limit backend is unavailable") from exc

        if not allowed:
            logger.warning(
                "rate limit denied",
                extra={
                    "operation": operation,
                    "identity_hash": identity_hash[:12],
                    "limit": limit,
                    "backend": "database",
                },
            )
        return RateLimitDecision(allowed, operation, limit, remaining, retry_after)

    async def enforce(
        self,
        operation: str,
        identity: str,
        limit: int,
        *,
        now: float | datetime | None = None,
    ) -> RateLimitDecision:
        try:
            decision = await self.check(operation, identity, limit, now=now)
        except RateLimitBackendUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate-limit service is unavailable; retry shortly.",
                headers={"Retry-After": "1"},
            ) from exc
        if not decision.allowed:
            _raise_denied(decision)
        return decision

    async def snapshot(self) -> dict[str, dict[str, int]]:
        try:
            async with self.database.session_scope() as session:
                rows = (
                    await session.execute(
                        select(
                            RateLimitCounter.operation,
                            func.sum(RateLimitCounter.allowed),
                            func.sum(RateLimitCounter.denied),
                        )
                        .group_by(RateLimitCounter.operation)
                        .order_by(RateLimitCounter.operation)
                    )
                ).all()
        except Exception as exc:
            raise RateLimitBackendUnavailable(
                "durable rate-limit counters are unavailable"
            ) from exc
        return {
            operation: {"allowed": int(allowed), "denied": int(denied)}
            for operation, allowed, denied in rows
        }

    async def ready(self) -> bool:
        try:
            async with self.database.session_scope() as session:
                await session.execute(select(RateLimitBucket.operation).limit(1))
                await session.execute(select(RateLimitCounter.operation).limit(1))
            return True
        except Exception:
            return False


__all__ = [
    "DatabaseSlidingWindowRateLimiter",
    "RateLimitBackendUnavailable",
    "RateLimitDecision",
    "RateLimiter",
    "SlidingWindowRateLimiter",
]
