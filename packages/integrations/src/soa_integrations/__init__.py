"""Shared, lightweight outbound-integration contracts.

The API and worker both import this package so a destination is tested
under the exact same allowlist/SSRF policy that protects real delivery.
"""

from soa_integrations.capabilities import IntegrationCapabilities, capabilities_for
from soa_integrations.connection import ConnectionTestRequest, ConnectionTestResult, test_connection
from soa_integrations.destination import DestinationRefusedError, validate_destination

__all__ = [
    "ConnectionTestRequest",
    "ConnectionTestResult",
    "DestinationRefusedError",
    "IntegrationCapabilities",
    "capabilities_for",
    "test_connection",
    "validate_destination",
]
