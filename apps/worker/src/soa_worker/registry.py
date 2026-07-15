"""Job-handler registry.

Handlers are async callables keyed by job type. Real job records and durable
claiming arrive with the JOB epic; the registry contract is stable now so
handlers written later do not change shape.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class JobEnvelope:
    """Minimal job shape consumed by handlers (extended by JOB-001)."""

    job_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    job_id: uuid.UUID | None = None


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
