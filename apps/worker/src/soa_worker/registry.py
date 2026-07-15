"""Job-handler registry.

Handlers are async callables keyed by the durable job type. The envelope
separates trusted queue metadata (job and tenant IDs) from untrusted payload
fields so failure callbacks cannot cross a tenant boundary.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class JobEnvelope:
    """Minimal durable job shape consumed by handlers."""

    job_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    job_id: uuid.UUID | None = None
    #: Tenant stamped on the durable queue row. Domain callbacks use this
    #: trusted value rather than accepting a tenant solely from payload JSON.
    organization_id: uuid.UUID | None = None
    #: Monotonic claim generation copied from ``jobs.attempts``. Durable queue
    #: acknowledgements use it as a fencing token so a stale handler cannot
    #: complete a lease that expired and was reclaimed, even by the same worker.
    lease_attempt: int | None = None


JobHandler = Callable[[JobEnvelope], Awaitable[None]]


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str) -> Callable[[JobHandler], JobHandler]:
        def decorator(handler: JobHandler) -> JobHandler:
            if job_type in self._handlers:
                raise ValueError(f"handler for job type {job_type!r} is already registered")
            self._handlers[job_type] = handler
            return handler

        return decorator

    def resolve(self, job_type: str) -> JobHandler:
        try:
            return self._handlers[job_type]
        except KeyError:
            raise KeyError(f"no handler registered for job type {job_type!r}") from None

    @property
    def registered_types(self) -> list[str]:
        return sorted(self._handlers)
