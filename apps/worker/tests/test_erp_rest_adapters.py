"""Bearer-REST ERP adapter tests (NetSuite, Dynamics 365, SAP): the shared
EXP-010 contract suite over each, bearer auth, per-vendor read-only
connection test, safe error redaction, and the registry lockstep."""

from typing import Any
from urllib.parse import urlparse

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
    host = urlparse(DELIVERY_URLS[slug]).hostname
    assert host is not None
    return AdapterDeliveryRequest(
        url=DELIVERY_URLS[slug],
        body=b'{"SalesOrder":"1"}',
        secret=TOKEN,
        business_key="export:i:d:r",
        attempt_number=1,
        timestamp=1_800_000_000,
        allowlist=[host],
        resolve=lambda _host: ["93.184.216.34"],
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
        assert capabilities.production_ready is False
        assert "not implemented" in capabilities.idempotency_mechanism.lower()

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_delivery_fails_closed_without_network(self, slug: str) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(201)

        async with client_for(handler) as client:
            result = await ADAPTERS[slug]().deliver(client, request_for(slug))
        assert result.outcome == "terminal_error"
        assert result.response_status is None
        assert result.safe_error is not None and "idempotent upsert" in result.safe_error
        assert calls == 0


class TestBearerRestContract(ErpAdapterContract):
    pass


class TestAuthAndDelivery:
    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_connection_probe_sends_bearer_auth_without_a_body(self, slug: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        async with client_for(handler) as client:
            result = await ADAPTERS[slug]().test_connection(client, request_for(slug))
        assert result.ok is True
        (sent,) = seen
        assert sent.method == "GET"
        assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
        assert sent.content == b""
        assert TOKEN not in str(sent.url)

    @pytest.mark.parametrize("slug", list(ADAPTERS))
    async def test_policy_refusal_blocks_probe_and_health(self, slug: str) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        original = request_for(slug)
        refused = AdapterDeliveryRequest(**{**original.__dict__, "allowlist": ["other.example"]})
        async with client_for(handler) as client:
            refused_delivery = await ADAPTERS[slug]().deliver(client, refused)
            assert refused_delivery.outcome == "terminal_error"
            assert refused_delivery.safe_error is not None
            assert "allowlisted" in refused_delivery.safe_error
            assert (await ADAPTERS[slug]().test_connection(client, refused)).ok is False
            assert (await ADAPTERS[slug]().health(client, refused)).status == "unreachable"
        assert calls == 0


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
