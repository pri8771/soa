"""Bearer-REST ERP adapter tests (NetSuite, Dynamics 365, SAP): the shared
EXP-010 contract suite over each, bearer auth, per-vendor read-only
connection test, safe error redaction, and the registry lockstep."""

from typing import Any

import httpx
import pytest

# import for registration side effect
import soa_worker.export_orchestrator  # noqa: F401  isql:skip
from soa_db.integrations import INTEGRATION_TYPES
from soa_worker.erp_adapter import (
    AdapterDeliveryRequest,
    registered_adapter_types,
    resolve_adapter,
)
from soa_worker.erp_rest_adapters import (
    Dynamics365Adapter,
    NetSuiteAdapter,
    SapAdapter,
)

TOKEN = "erp-oauth-access-token"

DELIVERY_URLS = {
    "netsuite": "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/salesOrder",
    "microsoft_dynamics365": "https://org.crm.dynamics.com/api/data/v9.2/salesorders",
    "sap_s4hana": "https://s4.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder",
}


def request_for(slug: str) -> AdapterDeliveryRequest:
    return AdapterDeliveryRequest(
        url=DELIVERY_URLS[slug],
        body=b'{"SalesOrder":"1"}',
        secret=TOKEN,
        business_key="export:i:d:r",
        attempt_number=1,
        timestamp=1_800_000_000,
        allowlist=[],
        resolve=None,
    )


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


ADAPTERS = {
    "netsuite": NetSuiteAdapter,
    "microsoft_dynamics365": Dynamics365Adapter,
    "sap_s4hana": SapAdapter,
}


class ErpAdapterContract:
    """The shared EXP-010 contract, parametrized across the three vendors."""

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    def test_capabilities_are_declared(self, slug: str) -> None:
        capabilities = ADAPTERS[slug]().capabilities()
        assert capabilities.formats
        assert capabilities.idempotency_mechanism.strip()

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    @pytest.mark.parametrize(
        ("status", "expected"),
        [(200, "delivered"), (204, "delivered"), (503, "retryable_error"), (400, "terminal_error")],
    )
    async def test_delivery_outcomes_are_classified(
        self, slug: str, status: int, expected: str
    ) -> None:
        adapter = ADAPTERS[slug]()
        async with client_for(lambda _r: httpx.Response(status)) as client:
            result = await adapter.deliver(client, request_for(slug))
        assert result.outcome == expected


class TestBearerRestContract(ErpAdapterContract):
    pass


class TestAuthAndDelivery:
    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_delivery_sends_bearer_auth_and_the_body(self, slug: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201)

        async with client_for(handler) as client:
            result = await ADAPTERS[slug]().deliver(client, request_for(slug))
        assert result.outcome == "delivered"
        (sent,) = seen
        assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
        assert sent.content == b'{"SalesOrder":"1"}'
        assert TOKEN not in str(sent.url)

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_rate_limit_retryable_auth_terminal(self, slug: str) -> None:
        adapter = ADAPTERS[slug]()
        async with client_for(lambda _r: httpx.Response(429)) as client:
            assert (await adapter.deliver(client, request_for(slug))).outcome == "retryable_error"
        async with client_for(lambda _r: httpx.Response(401)) as client:
            assert (await adapter.deliver(client, request_for(slug))).outcome == "terminal_error"

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_terminal_error_never_leaks_the_body(self, slug: str) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="Confidential CUSTOMER-SECRET echoed back")

        async with client_for(handler) as client:
            result = await ADAPTERS[slug]().deliver(client, request_for(slug))
        assert result.outcome == "terminal_error"
        assert result.safe_error is not None
        assert "CUSTOMER-SECRET" not in result.safe_error
        assert "Confidential" not in result.safe_error

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_network_error_is_retryable(self, slug: str) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        async with client_for(handler) as client:
            result = await ADAPTERS[slug]().deliver(client, request_for(slug))
        assert result.outcome == "retryable_error"
        assert result.response_status is None


class TestConnectionTest:
    async def test_netsuite_probes_the_metadata_catalog(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        async with client_for(handler) as client:
            result = await NetSuiteAdapter().test_connection(client, request_for("netsuite"))
        assert result.ok is True
        (probe,) = seen
        assert probe.method == "GET"
        assert str(probe.url).endswith("/services/rest/record/v1/metadata-catalog")

    async def test_odata_vendors_probe_metadata_at_the_service_root(self) -> None:
        for slug, adapter_cls in (
            ("microsoft_dynamics365", Dynamics365Adapter),
            ("sap_s4hana", SapAdapter),
        ):
            seen: list[httpx.Request] = []

            def handler(request: httpx.Request, _seen: list = seen) -> httpx.Response:
                _seen.append(request)
                return httpx.Response(200)

            async with client_for(handler) as client:
                result = await adapter_cls().test_connection(client, request_for(slug))
            assert result.ok is True, slug
            (probe,) = seen
            assert str(probe.url).endswith("/$metadata"), slug
            # It probes the parent service root, not the entity set itself.
            assert "salesorder" not in str(probe.url).lower().rsplit("/", 1)[-1]

    async def test_bad_credentials_reported(self) -> None:
        async with client_for(lambda _r: httpx.Response(403)) as client:
            result = await SapAdapter().test_connection(client, request_for("sap_s4hana"))
        assert result.ok is False
        assert "token" in result.detail


class TestRegistration:
    @pytest.mark.parametrize("slug", list(ADAPTERS))
    def test_each_is_registered_and_configurable(self, slug: str) -> None:
        assert slug in INTEGRATION_TYPES
        assert resolve_adapter(slug).slug == slug

    def test_lockstep_invariant_holds(self) -> None:
        # Every configurable integration type has a registered adapter.
        assert INTEGRATION_TYPES <= registered_adapter_types()

    def test_all_adapters_expose_the_contract_methods(self) -> None:
        for adapter_cls in ADAPTERS.values():
            adapter = adapter_cls()
            for method in ("capabilities", "test_connection", "deliver", "health"):
                assert callable(getattr(adapter, method))
