"""QuickBooks Online destination as an ERP adapter (EXP-011).

The first real ERP connector, implementing the EXP-010 generic contract
so orchestration reaches it exactly like the webhook adapter — a
registration, not a special case. It delivers a mapped payload to the
QuickBooks Online v3 REST API over OAuth2.

QuickBooks-specific facts baked in honestly:

- **No "SalesOrder" object.** QuickBooks Online has no sales-order
  entity; the sales-order equivalent for a seller acting on a customer PO
  is an **Estimate** (a non-posting quote). The delivery URL therefore
  targets `.../v3/company/{realmId}/estimate` by default; the mapping
  profile (EXP-002/003) produces the Estimate JSON, and a deployment that
  wants Invoices simply maps to and points at that resource instead. The
  adapter is transport + auth + classification; it does not invent the
  business object.
- **OAuth2 bearer auth.** The resolved credential (``request.secret``) is
  the OAuth2 ACCESS TOKEN and travels as ``Authorization: Bearer``. It is
  never logged and never echoed. Token refresh is the integration
  credential layer's job (a 401 is surfaced as terminal here — retrying
  the same expired token cannot succeed).
- **Idempotency via the RequestId query parameter.** QuickBooks
  deduplicates creates that carry the same ``requestid`` within its
  window; the adapter derives one from the export business key so a
  retried delivery does not create a duplicate Estimate.
- **Safe errors only.** A QuickBooks Fault response can echo submitted
  content; the adapter extracts only the fault code/type into the
  redacted summary and never surfaces the raw body or the token.

Classification matches the platform's retry semantics: 2xx delivered;
429 and 5xx retryable; 401/403 and other 4xx terminal (an auth or
validation problem retrying cannot fix).
"""

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from soa_integrations import (
    ConnectionTestRequest,
    DestinationRefusedError,
    capabilities_for,
    validate_destination,
)
from soa_integrations import (
    test_connection as run_connection_test,
)
from soa_worker.erp_adapter import (
    AdapterCapabilities,
    AdapterDeliveryRequest,
    AdapterDeliveryResult,
    AdapterHealth,
    ConnectionTestResult,
    register_adapter,
)

#: QuickBooks API minor version pinned for a stable field contract.
QBO_MINOR_VERSION = "70"

#: RequestId max length QuickBooks accepts.
_REQUEST_ID_MAX = 50


def _request_id(business_key: str) -> str:
    """A stable, QuickBooks-safe idempotency token from the export
    business key: alphanumeric only, bounded length."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", business_key)
    return cleaned[:_REQUEST_ID_MAX] or "soaexport"


def _with_params(url: str, business_key: str) -> str:
    """Set the connector-owned query parameters exactly once.

    Integration URLs are administrator-provided. Replacing any existing
    values prevents a configured URL from supplying a second ``requestid``
    that could override the stable export idempotency key at the vendor.
    """
    parsed = urlsplit(url)
    connector_keys = {"minorversion", "requestid"}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in connector_keys
    ]
    query.extend(
        (
            ("minorversion", QBO_MINOR_VERSION),
            ("requestid", _request_id(business_key)),
        )
    )
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _safe_fault(body: bytes, status: int) -> str:
    """Extract only the fault code/type from a QuickBooks error body —
    never the message (it can echo submitted content) or the token."""
    try:
        parsed = json.loads(body)
        fault = parsed.get("Fault", {})
        errors = fault.get("Error", [])
        if errors:
            first = errors[0]
            code = first.get("code", "unknown")
            fault_type = fault.get("type", "unknown")
            return (
                f"QuickBooks rejected the request (status {status}, "
                f"fault type {fault_type}, code {code})"
            )
    except (ValueError, AttributeError, KeyError, IndexError, TypeError):
        pass
    return f"QuickBooks rejected the request (status {status})"


class QuickBooksOnlineAdapter:
    slug = "quickbooks_online"

    def capabilities(self) -> AdapterCapabilities:
        shared = capabilities_for(self.slug)
        return AdapterCapabilities(
            supports_connection_test=shared.supports_connection_test,
            supports_health_check=shared.supports_health_check,
            idempotency_mechanism=shared.idempotency_mechanism,
            formats=("json",),
            production_ready=shared.production_ready,
        )

    def _headers(self, secret: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def test_connection(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> ConnectionTestResult:
        """A READ-ONLY probe: fetch CompanyInfo for the realm parsed from
        the delivery URL. It proves the token and company access without
        creating anything. If the realm cannot be parsed, say so rather
        than guessing."""
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
        url = _with_params(request.url, request.business_key)
        try:
            validate_destination(url, allowlist=request.allowlist, resolve=request.resolve)
        except DestinationRefusedError as refused:
            return AdapterDeliveryResult(
                outcome="terminal_error",
                response_status=None,
                safe_error=str(refused),
                redacted_response=None,
            )
        try:
            response = await client.post(
                url,
                content=request.body,
                headers=self._headers(request.secret),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            return AdapterDeliveryResult(
                outcome="retryable_error",
                response_status=None,
                safe_error="QuickBooks did not respond in time",
                redacted_response=None,
            )
        except httpx.HTTPError:
            return AdapterDeliveryResult(
                outcome="retryable_error",
                response_status=None,
                safe_error="QuickBooks could not be reached",
                redacted_response=None,
            )
        status = response.status_code
        if 200 <= status < 300:
            return AdapterDeliveryResult(
                outcome="delivered",
                response_status=status,
                safe_error=None,
                redacted_response="QuickBooks accepted the document",
            )
        if status == 429 or status >= 500:
            return AdapterDeliveryResult(
                outcome="retryable_error",
                response_status=status,
                safe_error=f"QuickBooks is temporarily unavailable (status {status})",
                redacted_response=None,
            )
        # 401/403 (auth) and other 4xx (validation) are terminal — retrying
        # an expired token or an invalid Estimate cannot succeed.
        return AdapterDeliveryResult(
            outcome="terminal_error",
            response_status=status,
            safe_error=_safe_fault(response.content, status),
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
        # A credential rejection is degraded (fixable by re-auth), an
        # unreachable API is unreachable.
        if "reached" in test.detail:
            return AdapterHealth(status="unreachable", detail=test.detail)
        return AdapterHealth(status="degraded", detail=test.detail)


register_adapter(QuickBooksOnlineAdapter())
