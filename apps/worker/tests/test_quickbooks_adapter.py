"""QuickBooks Online adapter tests (EXP-011): the reusable EXP-010
contract suite plus QuickBooks-specific behavior — OAuth2 bearer auth,
the RequestId idempotency parameter, read-only connection test, and safe
fault redaction that never leaks the token or submitted content."""

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from soa_db.integrations import INTEGRATION_TYPES
from soa_worker.erp_adapter import (
    AdapterDeliveryRequest,
    ErpAdapter,
    registered_adapter_types,
    resolve_adapter,
)
from soa_worker.quickbooks_adapter import QBO_MINOR_VERSION, QuickBooksOnlineAdapter

# import for registration side effect
import soa_worker.export_orchestrator  # noqa: F401  isort:skip

REALM = "9130350000000000"
BASE = "https://sandbox-quickbooks.api.intuit.com"
ESTIMATE_URL = f"{BASE}/v3/company/{REALM}/estimate"
TOKEN = "qbo-access-token-secret"


def request(url: str = ESTIMATE_URL, body: bytes = b'{"Line":[],"CustomerRef":{"value":"1"}}'):
    host = urlparse(url).hostname
    assert host is not None
    return AdapterDeliveryRequest(
        url=url,
        body=body,
        secret=TOKEN,
        business_key="export:int-1:doc-2:run-3",
        attempt_number=1,
        timestamp=1_800_000_000,
        allowlist=[host],
        resolve=lambda _host: ["93.184.216.34"],
    )


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- the shared EXP-010 contract suite, unchanged for the new adapter ----


class ErpAdapterContract:
    def make_adapter(self) -> ErpAdapter:
        raise NotImplementedError

    def test_capabilities_are_declared(self) -> None:
        capabilities = self.make_adapter().capabilities()
        assert capabilities.formats
        assert capabilities.idempotency_mechanism.strip()

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


class TestQuickBooksContract(ErpAdapterContract):
    def make_adapter(self) -> ErpAdapter:
        return QuickBooksOnlineAdapter()


# ---- QuickBooks-specific behavior ----


class TestAuthAndIdempotency:
    async def test_delivery_sends_bearer_auth_and_the_request_id(self) -> None:
        seen: list[httpx.Request] = []

        def handler(incoming: httpx.Request) -> httpx.Response:
            seen.append(incoming)
            return httpx.Response(200, json={"Estimate": {"Id": "42"}})

        async with client_for(handler) as client:
            result = await QuickBooksOnlineAdapter().deliver(client, request())
        assert result.outcome == "delivered"
        (sent,) = seen
        assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
        assert sent.headers["Accept"] == "application/json"
        # Idempotency + version params ride on the query string.
        assert f"minorversion={QBO_MINOR_VERSION}" in str(sent.url)
        assert "requestid=exportint1doc2run3" in str(sent.url)
        # The token never appears in the body/URL.
        assert TOKEN not in str(sent.url)

    async def test_request_id_is_stable_for_the_same_business_key(self) -> None:
        urls: list[str] = []

        def handler(incoming: httpx.Request) -> httpx.Response:
            urls.append(str(incoming.url))
            return httpx.Response(200)

        async with client_for(handler) as client:
            await QuickBooksOnlineAdapter().deliver(client, request())
            await QuickBooksOnlineAdapter().deliver(client, request())
        assert urls[0] == urls[1]  # a retry reuses the same RequestId

    async def test_connector_replaces_configured_idempotency_parameters(self) -> None:
        seen: list[httpx.Request] = []

        def handler(incoming: httpx.Request) -> httpx.Response:
            seen.append(incoming)
            return httpx.Response(200)

        configured = f"{ESTIMATE_URL}?requestid=attacker&minorversion=1&custom=kept"
        async with client_for(handler) as client:
            result = await QuickBooksOnlineAdapter().deliver(client, request(configured))
        assert result.outcome == "delivered"
        (sent,) = seen
        params = parse_qs(sent.url.query.decode())
        assert params["requestid"] == ["exportint1doc2run3"]
        assert params["minorversion"] == [QBO_MINOR_VERSION]
        assert params["custom"] == ["kept"]


class TestConnectionTest:
    async def test_connection_test_reads_companyinfo_never_writes(self) -> None:
        seen: list[httpx.Request] = []

        def handler(incoming: httpx.Request) -> httpx.Response:
            seen.append(incoming)
            return httpx.Response(200, json={"CompanyInfo": {"CompanyName": "Acme"}})

        async with client_for(handler) as client:
            result = await QuickBooksOnlineAdapter().test_connection(client, request())
        assert result.ok is True
        (probe,) = seen
        assert probe.method == "GET"
        assert f"/v3/company/{REALM}/companyinfo/{REALM}" in str(probe.url)

    async def test_connection_test_reports_bad_credentials(self) -> None:
        async with client_for(lambda _r: httpx.Response(401)) as client:
            result = await QuickBooksOnlineAdapter().test_connection(client, request())
        assert result.ok is False
        assert "token" in result.detail

    async def test_connection_test_without_a_parseable_realm_is_honest(self) -> None:
        async with client_for(lambda _r: httpx.Response(200)) as client:
            result = await QuickBooksOnlineAdapter().test_connection(
                client, request(url="https://example.com/not-quickbooks")
            )
        assert result.ok is False
        assert "realm" in result.detail

    async def test_connection_test_does_not_parse_a_realm_from_the_query_string(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        url = f"{BASE}/not-quickbooks?next=/v3/company/{REALM}/estimate"
        async with client_for(handler) as client:
            result = await QuickBooksOnlineAdapter().test_connection(client, request(url))
        assert result.ok is False
        assert "realm" in result.detail
        assert calls == 0

    async def test_connection_test_never_follows_redirects(self) -> None:
        seen: list[httpx.Request] = []

        def handler(incoming: httpx.Request) -> httpx.Response:
            seen.append(incoming)
            return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            result = await QuickBooksOnlineAdapter().test_connection(client, request())
        assert result.ok is False
        assert len(seen) == 1


class TestSafeErrors:
    async def test_fault_response_is_redacted_to_code_and_type_only(self) -> None:
        fault_body = {
            "Fault": {
                "Error": [
                    {
                        "code": "6240",
                        # A message that echoes submitted content — must NOT leak.
                        "Message": "Duplicate Document Number Error PO-SECRET-12345",
                        "Detail": "customer confidential text",
                    }
                ],
                "type": "ValidationFault",
            }
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=fault_body)

        async with client_for(handler) as client:
            result = await QuickBooksOnlineAdapter().deliver(client, request())
        assert result.outcome == "terminal_error"
        assert result.safe_error is not None
        assert "6240" in result.safe_error
        assert "ValidationFault" in result.safe_error
        # Neither the echoed content nor the detail leaks.
        assert "PO-SECRET-12345" not in result.safe_error
        assert "confidential" not in result.safe_error

    async def test_auth_failure_on_delivery_is_terminal(self) -> None:
        async with client_for(lambda _r: httpx.Response(401)) as client:
            result = await QuickBooksOnlineAdapter().deliver(client, request())
        assert result.outcome == "terminal_error"

    async def test_rate_limit_is_retryable(self) -> None:
        async with client_for(lambda _r: httpx.Response(429)) as client:
            result = await QuickBooksOnlineAdapter().deliver(client, request())
        assert result.outcome == "retryable_error"

    async def test_network_error_is_retryable(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        async with client_for(handler) as client:
            result = await QuickBooksOnlineAdapter().deliver(client, request())
        assert result.outcome == "retryable_error"
        assert result.response_status is None

    async def test_destination_policy_blocks_every_network_operation(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        refused = AdapterDeliveryRequest(
            **{
                **request().__dict__,
                "allowlist": ["other.example"],
            }
        )
        async with client_for(handler) as client:
            assert (await QuickBooksOnlineAdapter().deliver(client, refused)).outcome == (
                "terminal_error"
            )
            assert (await QuickBooksOnlineAdapter().test_connection(client, refused)).ok is False
            assert (await QuickBooksOnlineAdapter().health(client, refused)).status == (
                "unreachable"
            )
        assert calls == 0


class TestRegistration:
    def test_quickbooks_is_registered_and_configurable(self) -> None:
        assert "quickbooks_online" in INTEGRATION_TYPES
        assert resolve_adapter("quickbooks_online").slug == "quickbooks_online"
        # The lockstep invariant still holds: every configurable type has
        # an adapter.
        assert INTEGRATION_TYPES <= registered_adapter_types()

    async def test_health_maps_credential_vs_reachability(self) -> None:
        adapter = QuickBooksOnlineAdapter()
        async with client_for(lambda _r: httpx.Response(200, json={"CompanyInfo": {}})) as client:
            assert (await adapter.health(client, request())).status == "ok"
        async with client_for(lambda _r: httpx.Response(401)) as client:
            assert (await adapter.health(client, request())).status == "degraded"

        def unreachable(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        async with client_for(unreachable) as client:
            assert (await adapter.health(client, request())).status == "unreachable"
