"""Bearer-REST ERP adapters: NetSuite, Dynamics 365, SAP (EXP-011 family).

The ERP connectors after QuickBooks Online, all implementing the EXP-010
generic contract so orchestration reaches them like any other
destination. They share one shape — OAuth2 bearer auth over a JSON/OData
REST endpoint — so connection testing and fail-closed delivery behavior
live once in :class:`_BearerRestErpAdapter`; each vendor is a thin subclass
supplying only its slug and display name.

Same discipline as the QuickBooks adapter:

- the resolved credential (``request.secret``) is an OAuth2 ACCESS TOKEN,
  sent as ``Authorization: Bearer`` and never logged or echoed;
- the mapped payload (``request.body``) is produced by the mapping
  profile (EXP-002/003) — the adapter is transport, not business mapping;
- delivery is disabled until each vendor has an executable idempotent
  upsert/precondition contract; a generic POST would duplicate orders;
- error text is display-safe: only the status, never the vendor body
  (which can echo submitted content) or the token.

The connection test remains a READ-ONLY metadata request that proves the
token without creating anything. Passing it does not imply delivery is
production-ready.
"""

import httpx

from soa_integrations import (
    ConnectionTestRequest,
    DestinationRefusedError,
    capabilities_for,
    validate_destination,
)
from soa_integrations import test_connection as run_connection_test
from soa_worker.erp_adapter import (
    AdapterCapabilities,
    AdapterDeliveryRequest,
    AdapterDeliveryResult,
    AdapterHealth,
    ConnectionTestResult,
    register_adapter,
)


class _BearerRestErpAdapter:
    """Shared read-only probe and fail-closed ERP adapter behavior."""

    slug: str = ""
    vendor: str = ""

    def capabilities(self) -> AdapterCapabilities:
        shared = capabilities_for(self.slug)
        return AdapterCapabilities(
            supports_connection_test=shared.supports_connection_test,
            supports_health_check=shared.supports_health_check,
            idempotency_mechanism=shared.idempotency_mechanism,
            formats=("json",),
            production_ready=shared.production_ready,
        )

    async def test_connection(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> ConnectionTestResult:
        result = await run_connection_test(
            client,
            self.slug,
            ConnectionTestRequest(
                url=request.url,
                secret=request.secret,
                business_key=request.business_key,
                timestamp=request.timestamp,
                allowlist=request.allowlist,
                resolve=request.resolve,
            ),
        )
        return ConnectionTestResult(ok=result.ok, detail=result.detail)

    async def deliver(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterDeliveryResult:
        try:
            validate_destination(
                request.url,
                allowlist=request.allowlist,
                resolve=request.resolve,
            )
        except DestinationRefusedError as refused:
            return AdapterDeliveryResult(
                outcome="terminal_error",
                response_status=None,
                safe_error=str(refused),
                redacted_response=None,
            )
        # These connectors can prove reachability, but their vendor-specific
        # idempotent create/upsert contracts are not implemented. A generic
        # POST can duplicate an order on retry, so delivery fails closed.
        return AdapterDeliveryResult(
            outcome="terminal_error",
            response_status=None,
            safe_error=f"{self.vendor} delivery is disabled until idempotent upsert is implemented",
            redacted_response=None,
        )

    async def health(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterHealth:
        try:
            validate_destination(
                request.url,
                allowlist=request.allowlist,
                resolve=request.resolve,
            )
        except DestinationRefusedError as refused:
            return AdapterHealth(status="unreachable", detail=str(refused))
        test = await self.test_connection(client, request)
        if test.ok:
            return AdapterHealth(status="ok", detail=test.detail)
        if "reached" in test.detail:
            return AdapterHealth(status="unreachable", detail=test.detail)
        return AdapterHealth(status="degraded", detail=test.detail)


class NetSuiteAdapter(_BearerRestErpAdapter):
    """NetSuite read-only probe; order delivery is not implemented."""

    slug = "netsuite"
    vendor = "NetSuite"


class Dynamics365Adapter(_BearerRestErpAdapter):
    """Dynamics 365 read-only probe; order delivery is not implemented."""

    slug = "microsoft_dynamics365"
    vendor = "Dynamics 365"


class SapAdapter(_BearerRestErpAdapter):
    """SAP S/4HANA read-only probe; order delivery is not implemented."""

    slug = "sap_s4hana"
    vendor = "SAP"


def register_rest_erp_adapters() -> None:
    """Register all bearer-REST ERP adapters. Importing this module calls
    it; safe to call once at import."""
    register_adapter(NetSuiteAdapter())
    register_adapter(Dynamics365Adapter())
    register_adapter(SapAdapter())


register_rest_erp_adapters()


__all__ = [
    "Dynamics365Adapter",
    "NetSuiteAdapter",
    "SapAdapter",
    "register_rest_erp_adapters",
]
