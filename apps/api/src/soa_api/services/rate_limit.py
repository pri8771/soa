"""Rate limiting and abuse controls (SEC-003).

A per-application sliding-window limiter keyed by (operation,
identity). Callers name the OPERATION ("uploads", "download_urls", ...)
and the IDENTITY that should be throttled — principal, credential, or
client IP depending on the surface — so one flood cannot starve other
tenants or other operations.

Semantics, stated:

- **safe backpressure** — a denial is HTTP 429 with ``Retry-After`` and
  ``X-RateLimit-*`` headers; nothing is queued, dropped silently, or
  half-processed. Denied requests are never recorded as usage, so a
  client that backs off recovers exactly after the window drains.
- **observable** — every allow/deny increments a per-operation counter
  (``snapshot()``), and each denial logs the operation with a hash of
  the identity (the identity itself stays out of logs).
- **honest scope** — the window lives in this PROCESS. Horizontal
  replicas each grant their own budget; a shared backend (OPEN-001
  infra) slots behind the same interface when multi-replica deployment
  arrives. Limits here are abuse controls, not billing quotas
  (ANA-009 owns those).
"""

import hashlib
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


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


@dataclass
class SlidingWindowRateLimiter:
    """Sliding 60-second windows per (operation, identity)."""

    window_seconds: float = 60.0
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _counters: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"allowed": 0, "denied": 0})
    )

    def check(
        self, operation: str, identity: str, limit: int, *, now: float | None = None
    ) -> RateLimitDecision:
        current = now if now is not None else time.monotonic()
        key = f"{operation}:{identity}"
        events = self._events[key]
        while events and current - events[0] > self.window_seconds:
            events.popleft()
        if len(events) >= limit:
            self._counters[operation]["denied"] += 1
            retry_after = max(1, int(self.window_seconds - (current - events[0])) + 1)
            logger.warning(
                "rate limit denied",
                extra={
                    "operation": operation,
                    "identity_hash": hashlib.sha256(identity.encode()).hexdigest()[:12],
                    "limit": limit,
                },
            )
            return RateLimitDecision(
                allowed=False,
                operation=operation,
                limit=limit,
                remaining=0,
                retry_after_seconds=retry_after,
            )
        events.append(current)
        self._counters[operation]["allowed"] += 1
        return RateLimitDecision(
            allowed=True,
            operation=operation,
            limit=limit,
            remaining=limit - len(events),
            retry_after_seconds=0,
        )

    def enforce(
        self, operation: str, identity: str, limit: int, *, now: float | None = None
    ) -> RateLimitDecision:
        """check(), raising 429 with backoff headers on denial."""
        decision = self.check(operation, identity, limit, now=now)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for {operation} — retry in "
                    f"{decision.retry_after_seconds}s."
                ),
                headers=decision.headers(),
            )
        return decision

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Per-operation allowed/denied totals since process start —
        identities are never included."""
        return {operation: dict(counts) for operation, counts in sorted(self._counters.items())}


__all__ = ["RateLimitDecision", "SlidingWindowRateLimiter"]
