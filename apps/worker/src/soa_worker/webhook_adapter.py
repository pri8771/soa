"""The webhook destination as an ERP adapter (EXP-010).

Wraps the EXP-007 signed-webhook delivery in the generic adapter
contract, and registers itself as the ``webhook`` integration type.
Connection tests send a SIGNED test event (never an order) so the
receiver can prove signature verification end to end before any real
payload flows.
"""

import json

import httpx

from soa_worker.erp_adapter import (
    AdapterCapabilities,
    AdapterDeliveryRequest,
    AdapterDeliveryResult,
    AdapterHealth,
    ConnectionTestResult,
    register_adapter,
)
from soa_worker.webhook import DestinationRefusedError, deliver_webhook, validate_destination


class WebhookAdapter:
    slug = "webhook"

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_connection_test=True,
            supports_health_check=True,
            idempotency_mechanism="X-SOA-Idempotency-Key header (business key)",
            formats=("json",),
        )

    async def test_connection(
        self, client: httpx.AsyncClient, request: AdapterDeliveryRequest
    ) -> ConnectionTestResult:
        """A signed PING — destination policy enforced, never an order."""
        ping = AdapterDeliveryRequest(
            url=request.url,
            body=json.dumps({"type": "soa.connection_test"}).encode("utf-8"),
            secret=request.secret,
            business_key=f"connection-test:{request.business_key}",
            attempt_number=1,
            timestamp=request.timestamp,
            allowlist=request.allowlist,
            resolve=request.resolve,
        )
        result = await self.deliver(client, ping)
        if result.outcome == "delivered":
            return ConnectionTestResult(ok=True, detail="receiver accepted a signed test event")
        return ConnectionTestResult(
            ok=False, detail=result.safe_error or "receiver did not accept the test event"
        )

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
        return AdapterHealth(status="degraded", detail=test.detail)


register_adapter(WebhookAdapter())
