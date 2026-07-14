"""Bearer-REST ERP adapters: NetSuite, Dynamics 365, SAP (EXP-011 family).

The ERP connectors after QuickBooks Online, all implementing the EXP-010
generic contract so orchestration reaches them like any other
destination. They share one shape — OAuth2 bearer auth over a JSON/OData
REST endpoint — so the transport, auth, response classification, and safe
error handling live ONCE in :class:`_BearerRestErpAdapter`; each vendor is
a thin subclass supplying its slug, idempotency mechanism, and a
read-only connection-test URL.

Same discipline as the QuickBooks adapter:

- the resolved credential (``request.secret``) is an OAuth2 ACCESS TOKEN,
  sent as ``Authorization: Bearer`` and never logged or echoed;
- the mapped payload (``request.body``) is produced by the mapping
  profile (EXP-002/003) — the adapter is transport, not business mapping;
- delivery is classified 2xx delivered / 429+5xx retryable / other 4xx
  (auth or validation) terminal;
- error text is display-safe: only the status, never the vendor body
  (which can echo submitted content) or the token.

Idempotency is each vendor's native mechanism, set by the mapping on the
payload (an external id / alternate key / ETag), so a retried delivery
does not create a duplicate order. The connection test is a READ-ONLY
metadata request that proves the token without creating anything.
"""

import httpx

from soa_worker.erp_adapter import (
    AdapterCapabilities,
    AdapterDeliveryRequest,
    AdapterDeliveryResult,
    AdapterHealth,
    ConnectionTestResult,
    register_adapter,
)


class _BearerRestErpAdapter:
    """Shared transport for OAuth2-bearer JSON/OData ERP endpoints."""

    slug: str = ""
    vendor: str = ""
    idempotency: str = ""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_connection_test=True,
            supports_health_check=True,
            idempotency_mechanism=self.idempotency,
            formats=("json",),
        )

    def _headers(self, secret: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _connection_test_url(self, delivery_url: str) -> str | None:
        """A read-only metadata URL derived from the delivery URL, or None
        if one cannot be determined. Overridden per vendor."""
        raise NotImplementedError

    async def test_connection(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> ConnectionTestResult:
        url = self._connection_test_url(request.url)
        if url is None:
            return ConnectionTestResult(
                ok=False,
                detail=f"could not derive a {self.vendor} metadata URL from the endpoint",
            )
        try:
            response = await client.get(url, headers=self._headers(request.secret))
        except httpx.HTTPError:
            return ConnectionTestResult(ok=False, detail=f"{self.vendor} could not be reached")
        if response.status_code < 300:
            return ConnectionTestResult(ok=True, detail=f"{self.vendor} accepted the credentials")
        if response.status_code in (401, 403):
            return ConnectionTestResult(
                ok=False,
                detail=f"{self.vendor} rejected the credentials (token invalid or expired)",
            )
        return ConnectionTestResult(
            ok=False, detail=f"{self.vendor} returned status {response.status_code}"
        )

    async def deliver(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterDeliveryResult:
        try:
            response = await client.post(
                request.url, content=request.body, headers=self._headers(request.secret)
            )
        except httpx.TimeoutException:
            return AdapterDeliveryResult(
                outcome="retryable_error",
                response_status=None,
                safe_error=f"{self.vendor} did not respond in time",
                redacted_response=None,
            )
        except httpx.HTTPError:
            return AdapterDeliveryResult(
                outcome="retryable_error",
                response_status=None,
                safe_error=f"{self.vendor} could not be reached",
                redacted_response=None,
            )
        status = response.status_code
        if 200 <= status < 300:
            return AdapterDeliveryResult(
                outcome="delivered",
                response_status=status,
                safe_error=None,
                redacted_response=f"{self.vendor} accepted the document",
            )
        if status == 429 or status >= 500:
            return AdapterDeliveryResult(
                outcome="retryable_error",
                response_status=status,
                safe_error=f"{self.vendor} is temporarily unavailable (status {status})",
                redacted_response=None,
            )
        # 401/403 (auth) and other 4xx (validation) are terminal — the
        # body can echo submitted content, so only the status is surfaced.
        return AdapterDeliveryResult(
            outcome="terminal_error",
            response_status=status,
            safe_error=(
                f"{self.vendor} rejected the request (status {status}) — "
                "check the token and the mapped payload"
            ),
            redacted_response=None,
        )

    async def health(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterHealth:
        test = await self.test_connection(client, request)
        if test.ok:
            return AdapterHealth(status="ok", detail=test.detail)
        if "reached" in test.detail:
            return AdapterHealth(status="unreachable", detail=test.detail)
        return AdapterHealth(status="degraded", detail=test.detail)


class NetSuiteAdapter(_BearerRestErpAdapter):
    """NetSuite SuiteTalk REST Record API. NetSuite HAS a native
    ``salesOrder`` record; idempotency is an upsert by external id
    (``PUT .../salesOrder/eid:{externalId}``), which the mapping sets."""

    slug = "netsuite"
    vendor = "NetSuite"
    idempotency = "external id upsert (PUT .../eid:{externalId})"

    def _connection_test_url(self, delivery_url: str) -> str | None:
        marker = "/services/rest/record/"
        index = delivery_url.find(marker)
        if index < 0:
            return None
        base = delivery_url[:index]
        return f"{base}/services/rest/record/v1/metadata-catalog"


class Dynamics365Adapter(_BearerRestErpAdapter):
    """Microsoft Dynamics 365 (Business Central / F&O) OData Web API.
    Idempotency is an alternate-key upsert / ``If-None-Match`` set by the
    mapping. The connection test reads the OData ``$metadata`` at the
    service root."""

    slug = "microsoft_dynamics365"
    vendor = "Dynamics 365"
    idempotency = "alternate-key upsert (PATCH by key, If-None-Match)"

    def _connection_test_url(self, delivery_url: str) -> str | None:
        # OData: the entity set is the last path segment; the service root
        # is the parent, and metadata lives at <root>/$metadata.
        root = delivery_url.split("?", 1)[0].rsplit("/", 1)[0]
        if not root:
            return None
        return f"{root}/$metadata"


class SapAdapter(_BearerRestErpAdapter):
    """SAP S/4HANA OData sales-order API (e.g. ``API_SALES_ORDER_SRV``).
    Idempotency is the OData ETag (``If-Match``) / an external reference
    the mapping sets. The connection test reads the service ``$metadata``."""

    slug = "sap_s4hana"
    vendor = "SAP"
    idempotency = "OData ETag (If-Match) / external reference"

    def _connection_test_url(self, delivery_url: str) -> str | None:
        root = delivery_url.split("?", 1)[0].rsplit("/", 1)[0]
        if not root:
            return None
        return f"{root}/$metadata"


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
