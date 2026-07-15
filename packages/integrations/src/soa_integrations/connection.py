"""Read-only/synthetic connection tests shared by the API and worker."""

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from soa_integrations.destination import DestinationRefusedError, validate_destination

_QBO_REALM = re.compile(r"/v3/company/([^/]+)/")
_QBO_MINOR_VERSION = "70"


@dataclass(frozen=True)
class ConnectionTestRequest:
    url: str
    secret: str
    business_key: str
    timestamp: int
    allowlist: Sequence[str]
    resolve: Callable[[str], list[str]] | None = None


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    detail: str


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}", "Accept": "application/json"}


def _metadata_url(integration_type: str, delivery_url: str) -> str | None:
    parsed = urlsplit(delivery_url)
    if integration_type == "netsuite":
        marker = "/services/rest/record/"
        index = parsed.path.find(marker)
        if index < 0:
            return None
        return urlunsplit(
            parsed._replace(
                path=f"{parsed.path[:index]}/services/rest/record/v1/metadata-catalog",
                query="",
                fragment="",
            )
        )
    if integration_type in {"microsoft_dynamics365", "sap_s4hana"}:
        root = parsed.path.rsplit("/", 1)[0]
        if not root:
            return None
        return urlunsplit(parsed._replace(path=f"{root}/$metadata", query="", fragment=""))
    return None


async def _safe_get(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    vendor: str,
) -> ConnectionTestResult:
    try:
        response = await client.get(
            url,
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return ConnectionTestResult(ok=False, detail=f"{vendor} could not be reached")
    if 200 <= response.status_code < 300:
        return ConnectionTestResult(ok=True, detail=f"{vendor} accepted the credentials")
    if response.status_code in (401, 403):
        return ConnectionTestResult(
            ok=False,
            detail=f"{vendor} rejected the credentials (token invalid or expired)",
        )
    return ConnectionTestResult(ok=False, detail=f"{vendor} returned status {response.status_code}")


async def test_connection(
    client: httpx.AsyncClient,
    integration_type: str,
    request: ConnectionTestRequest,
) -> ConnectionTestResult:
    """Run the connector's non-order connection test under destination policy."""
    try:
        if integration_type == "webhook":
            validate_destination(request.url, allowlist=request.allowlist, resolve=request.resolve)
            body = json.dumps({"type": "soa.connection_test"}, separators=(",", ":")).encode()
            digest = hmac.new(
                request.secret.encode(),
                f"{request.timestamp}.".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-SOA-Signature": f"t={request.timestamp},v1={digest}",
                "X-SOA-Idempotency-Key": f"connection-test:{request.business_key}",
                "X-SOA-Delivery-Attempt": "1",
            }
            try:
                response = await client.post(
                    request.url,
                    content=body,
                    headers=headers,
                    timeout=10.0,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                return ConnectionTestResult(ok=False, detail="receiver could not be reached")
            if 200 <= response.status_code < 300:
                return ConnectionTestResult(ok=True, detail="receiver accepted a signed test event")
            return ConnectionTestResult(
                ok=False, detail=f"receiver returned status {response.status_code}"
            )

        if integration_type == "quickbooks_online":
            validate_destination(
                request.url,
                allowlist=request.allowlist,
                resolve=request.resolve,
            )
            parsed = urlsplit(request.url)
            match = _QBO_REALM.search(parsed.path)
            if match is None:
                return ConnectionTestResult(
                    ok=False,
                    detail=(
                        "could not determine the QuickBooks company (realm) id from the endpoint"
                    ),
                )
            realm = match.group(1)
            url = urlunsplit(
                parsed._replace(
                    path=(f"{parsed.path[: match.start()]}/v3/company/{realm}/companyinfo/{realm}"),
                    query=urlencode({"minorversion": _QBO_MINOR_VERSION}),
                    fragment="",
                )
            )
            validate_destination(url, allowlist=request.allowlist, resolve=request.resolve)
            return await _safe_get(
                client,
                url=url,
                headers=_bearer(request.secret),
                vendor="QuickBooks",
            )

        vendors = {
            "netsuite": "NetSuite",
            "microsoft_dynamics365": "Dynamics 365",
            "sap_s4hana": "SAP",
        }
        if integration_type in vendors:
            validate_destination(
                request.url,
                allowlist=request.allowlist,
                resolve=request.resolve,
            )
            metadata_url = _metadata_url(integration_type, request.url)
            if metadata_url is None:
                return ConnectionTestResult(
                    ok=False,
                    detail=(
                        f"could not derive a {vendors[integration_type]} metadata URL "
                        "from the endpoint"
                    ),
                )
            validate_destination(metadata_url, allowlist=request.allowlist, resolve=request.resolve)
            return await _safe_get(
                client,
                url=metadata_url,
                headers=_bearer(request.secret),
                vendor=vendors[integration_type],
            )
        return ConnectionTestResult(ok=False, detail="this integration type is not supported")
    except DestinationRefusedError as refused:
        return ConnectionTestResult(ok=False, detail=str(refused))
