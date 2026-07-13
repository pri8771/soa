"""ERP adapter contract tests (EXP-010): the reusable contract suite run
against the webhook adapter, registry fail-closed behavior, and the
structural guarantee that orchestration is destination-agnostic."""

import inspect
import json
from typing import Any

import httpx
import pytest

import soa_worker.export_orchestrator as orchestrator_module
from soa_db.integrations import INTEGRATION_TYPES
from soa_worker.erp_adapter import (
    AdapterDeliveryRequest,
    ErpAdapter,
    UnknownAdapterError,
    register_adapter,
    registered_adapter_types,
    resolve_adapter,
)
from soa_worker.webhook import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, verify_webhook_signature
from soa_worker.webhook_adapter import WebhookAdapter

NOW = 1_800_000_000
ALLOWLIST = ["erp.northstar.example"]
SECRET = "whsec_adapter_secret"


def request(body: bytes = b'{"PoNumber":"PO-1"}') -> AdapterDeliveryRequest:
    return AdapterDeliveryRequest(
        url="https://erp.northstar.example/orders",
        body=body,
        secret=SECRET,
        business_key="export:i:d:r",
        attempt_number=1,
        timestamp=NOW,
        allowlist=ALLOWLIST,
        resolve=lambda _host: ["93.184.216.34"],
    )


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class ErpAdapterContract:
    """The contract every destination adapter must satisfy. A new ERP
    adapter (EXP-011) subclasses this with its own factory and passes
    unchanged."""

    def make_adapter(self) -> ErpAdapter:
        raise NotImplementedError

    def test_capabilities_are_declared(self) -> None:
        capabilities = self.make_adapter().capabilities()
        assert capabilities.formats, "an adapter must name at least one format"
        assert capabilities.idempotency_mechanism.strip(), (
            "an adapter must say HOW the destination deduplicates redeliveries"
        )

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(200, "delivered"), (503, "retryable_error"), (400, "terminal_error")],
    )
    async def test_delivery_outcomes_are_classified(self, status: int, expected: str) -> None:
        adapter = self.make_adapter()
        async with client_for(lambda _r: httpx.Response(status)) as client:
            result = await adapter.deliver(client, request())
        assert result.outcome == expected
        assert result.response_status == status

    async def test_connection_test_is_honest(self) -> None:
        adapter = self.make_adapter()
        if not adapter.capabilities().supports_connection_test:
            pytest.skip("adapter declares no connection test")
        async with client_for(lambda _r: httpx.Response(200)) as client:
            assert (await adapter.test_connection(client, request())).ok is True
        async with client_for(lambda _r: httpx.Response(500)) as client:
            failed = await adapter.test_connection(client, request())
        assert failed.ok is False
        assert failed.detail  # says why


class TestWebhookAdapterContract(ErpAdapterContract):
    def make_adapter(self) -> ErpAdapter:
        return WebhookAdapter()

    async def test_connection_test_sends_a_signed_test_event_never_an_order(self) -> None:
        seen: list[httpx.Request] = []

        def handler(incoming: httpx.Request) -> httpx.Response:
            seen.append(incoming)
            return httpx.Response(200)

        adapter = WebhookAdapter()
        order_body = b'{"PoNumber":"PO-REAL-ORDER"}'
        async with client_for(handler) as client:
            await adapter.test_connection(client, request(body=order_body))
        (ping,) = seen
        assert json.loads(ping.content) == {"type": "soa.connection_test"}
        assert b"PO-REAL-ORDER" not in ping.content
        assert ping.headers[IDEMPOTENCY_HEADER].startswith("connection-test:")
        assert verify_webhook_signature(
            SECRET, ping.content, ping.headers[SIGNATURE_HEADER], now=NOW
        )

    async def test_policy_refusals_are_terminal_not_retryable(self) -> None:
        adapter = WebhookAdapter()
        refused_request = AdapterDeliveryRequest(
            url="https://evil.example/orders",  # not on the allowlist
            body=b"{}",
            secret=SECRET,
            business_key="k",
            attempt_number=1,
            timestamp=NOW,
            allowlist=ALLOWLIST,
        )
        async with client_for(lambda _r: httpx.Response(200)) as client:
            result = await adapter.deliver(client, refused_request)
        assert result.outcome == "terminal_error"
        assert result.safe_error is not None and "allowlist" in result.safe_error

    async def test_health_reports_policy_and_reachability(self) -> None:
        adapter = WebhookAdapter()
        async with client_for(lambda _r: httpx.Response(200)) as client:
            healthy = await adapter.health(client, request())
        assert healthy.status == "ok"
        async with client_for(lambda _r: httpx.Response(500)) as client:
            degraded = await adapter.health(client, request())
        assert degraded.status == "degraded"
        bad = AdapterDeliveryRequest(
            url="http://erp.northstar.example/orders",  # not https
            body=b"{}",
            secret=SECRET,
            business_key="k",
            attempt_number=1,
            timestamp=NOW,
            allowlist=ALLOWLIST,
        )
        async with client_for(lambda _r: httpx.Response(200)) as client:
            unreachable = await adapter.health(client, bad)
        assert unreachable.status == "unreachable"


def test_registry_fails_closed_and_covers_every_configurable_type() -> None:
    assert resolve_adapter("webhook").slug == "webhook"
    with pytest.raises(UnknownAdapterError, match="sap-rfc"):
        resolve_adapter("sap-rfc")
    # Every integration type a tenant can configure has an adapter.
    assert INTEGRATION_TYPES <= registered_adapter_types()


def test_a_new_adapter_registers_without_touching_orchestration() -> None:
    """The EXP-010 acceptance, structurally: orchestration resolves
    adapters from the registry and contains no destination-specific
    delivery call — a new ERP is a registration, not a refactor."""

    class DummyErpAdapter(WebhookAdapter):
        slug = "test-dummy-erp"

    register_adapter(DummyErpAdapter())
    assert resolve_adapter("test-dummy-erp").slug == "test-dummy-erp"
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(DummyErpAdapter())

    source = inspect.getsource(orchestrator_module)
    assert "resolve_adapter" in source
    assert "deliver_webhook" not in source  # no destination-specific path
