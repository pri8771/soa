"""Generic ERP adapter contract (EXP-010).

Every outbound destination type implements ONE protocol: declare
capabilities, test the connection, map (delegated to the deterministic
engine unless the destination needs something exotic), deliver, and
report health. Export orchestration resolves the adapter from the
registry by integration type and never special-cases a destination —
adding the first real ERP (EXP-011) means registering a new adapter,
not touching orchestration.

The registry fails closed and is kept in lockstep with
``soa_db.integrations.INTEGRATION_TYPES`` (a test enforces it): a type
without an adapter cannot be configured, and an adapter without a type
cannot be reached.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class AdapterCapabilities:
    """What this destination type can do — orchestration and the UI read
    these instead of guessing."""

    supports_connection_test: bool
    supports_health_check: bool
    #: How the destination deduplicates redeliveries (e.g. an
    #: idempotency header, an external-id field on the payload).
    idempotency_mechanism: str
    formats: tuple[str, ...]


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class AdapterHealth:
    status: str  # ok | degraded | unreachable | unknown
    detail: str


@dataclass(frozen=True)
class AdapterDeliveryRequest:
    """Everything one delivery needs — resolved by orchestration, opaque
    to it. The body is the encoded export artifact; the business key is
    the idempotency boundary; the secret never outlives the call."""

    url: str
    body: bytes
    secret: str
    business_key: str
    attempt_number: int
    timestamp: int
    allowlist: Sequence[str]
    resolve: Callable[[str], list[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterDeliveryResult:
    outcome: str  # delivered | retryable_error | terminal_error
    response_status: int | None
    safe_error: str | None
    redacted_response: str | None


class ErpAdapter(Protocol):
    """The contract every destination adapter satisfies."""

    slug: str

    def capabilities(self) -> AdapterCapabilities: ...

    async def test_connection(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> ConnectionTestResult: ...

    async def deliver(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterDeliveryResult: ...

    async def health(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterHealth: ...


class UnknownAdapterError(Exception):
    def __init__(self, integration_type: str, known: Sequence[str]) -> None:
        super().__init__(
            f"no adapter registered for integration type {integration_type!r} — "
            f"registered: {', '.join(sorted(known)) or '(none)'}"
        )


_REGISTRY: dict[str, ErpAdapter] = {}


def register_adapter(adapter: ErpAdapter) -> None:
    if adapter.slug in _REGISTRY:
        raise ValueError(f"adapter {adapter.slug!r} is already registered")
    _REGISTRY[adapter.slug] = adapter


def resolve_adapter(integration_type: str) -> ErpAdapter:
    try:
        return _REGISTRY[integration_type]
    except KeyError:
        raise UnknownAdapterError(integration_type, list(_REGISTRY)) from None


def registered_adapter_types() -> frozenset[str]:
    return frozenset(_REGISTRY)
