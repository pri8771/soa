"""The webhook destination as an ERP adapter (EXP-010).

Wraps the EXP-007 signed-webhook delivery in the generic adapter
contract, and registers itself as the ``webhook`` integration type.
Connection tests send a SIGNED test event (never an order) so the
receiver can prove signature verification end to end before any real
payload flows.
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
from soa_worker.webhook import deliver_webhook


class WebhookAdapter:
    slug = "webhook"

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
        """A signed PING — destination policy enforced, never an order."""
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
            result = await deliver_webhook(
                client,
                url=request.url,
                body=request.body,
                secret=request.secret,
                business_key=request.business_key,
                attempt_number=request.attempt_number,
                timestamp=request.timestamp,
                allowlist=request.allowlist,
                resolve=request.resolve,
            )
        except DestinationRefusedError as refused:
            # Policy refusals are terminal: retrying cannot fix an
            # unallowlisted or private destination.
            return AdapterDeliveryResult(
                outcome="terminal_error",
                response_status=None,
                safe_error=str(refused),
                redacted_response=None,
            )
        return AdapterDeliveryResult(
            outcome=result.outcome,
            response_status=result.response_status,
            safe_error=result.safe_error,
            redacted_response=result.redacted_response,
        )

    async def health(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> AdapterHealth:
        try:
            validate_destination(request.url, allowlist=request.allowlist, resolve=request.resolve)
        except DestinationRefusedError as refused:
            return AdapterHealth(status="unreachable", detail=str(refused))
        test = await self.test_connection(client, request)
        if test.ok:
            return AdapterHealth(status="ok", detail=test.detail)
        if "reached" in test.detail:
            return AdapterHealth(status="unreachable", detail=test.detail)
        return AdapterHealth(status="degraded", detail=test.detail)


register_adapter(WebhookAdapter())
