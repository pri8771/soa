"""Truthful capability registry for configurable outbound destinations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class IntegrationCapabilities:
    supports_connection_test: bool
    supports_health_check: bool
    production_ready: bool
    idempotency_mechanism: str
    readiness_detail: str


_CAPABILITIES = {
    "webhook": IntegrationCapabilities(
        supports_connection_test=True,
        supports_health_check=True,
        production_ready=True,
        idempotency_mechanism="X-SOA-Idempotency-Key header (business key)",
        readiness_detail="Signed webhook delivery is production-ready.",
    ),
    "quickbooks_online": IntegrationCapabilities(
        supports_connection_test=True,
        supports_health_check=True,
        production_ready=True,
        idempotency_mechanism="RequestId query parameter (from the export business key)",
        readiness_detail="QuickBooks delivery uses a stable RequestId and is production-ready.",
    ),
    "netsuite": IntegrationCapabilities(
        supports_connection_test=True,
        supports_health_check=True,
        production_ready=False,
        idempotency_mechanism="Not implemented; delivery is disabled.",
        readiness_detail="A vendor-specific external-id upsert contract is still required.",
    ),
    "microsoft_dynamics365": IntegrationCapabilities(
        supports_connection_test=True,
        supports_health_check=True,
        production_ready=False,
        idempotency_mechanism="Not implemented; delivery is disabled.",
        readiness_detail="A vendor-specific alternate-key upsert contract is still required.",
    ),
    "sap_s4hana": IntegrationCapabilities(
        supports_connection_test=True,
        supports_health_check=True,
        production_ready=False,
        idempotency_mechanism="Not implemented; delivery is disabled.",
        readiness_detail="No proven SAP idempotency contract exists; delivery fails closed.",
    ),
}


def capabilities_for(integration_type: str) -> IntegrationCapabilities:
    try:
        return _CAPABILITIES[integration_type]
    except KeyError:
        raise ValueError(f"unknown integration type {integration_type!r}") from None
