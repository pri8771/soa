"""Built-in provider registrations (AIO-001).

Importing this module (via ``soa_worker.providers``) registers every
adapter that ships with the worker itself. Today that is the PRC-006
deterministic mock extraction provider — it runs entirely locally, so
its data policy is the strict local one. Real adapters (AIO-002+)
register here as they land; hosted adapters whose credentials are
missing at startup must NOT register (unavailable credentials disable
the capability clearly, per AIO-006).
"""

from typing import Any

from soa_worker.extraction.mock import PROVIDER_NAME, MockExtractionProvider
from soa_worker.providers.capabilities import (
    ANY_LANGUAGE,
    LOCAL_DATA_POLICY,
    Capability,
    ProviderInfo,
    register_provider,
)

register_provider(
    ProviderInfo(
        name=PROVIDER_NAME,
        capability=Capability.FIELD_EXTRACTION,
        languages=(ANY_LANGUAGE,),
        data_policy=LOCAL_DATA_POLICY,
    ),
    MockExtractionProvider,
)

# Native PDF text (AIO-002): runs in the local sandbox; text is not a
# language-bound capability, so it declares the wildcard. The factory
# imports lazily — the adapter module imports this package, so an eager
# import here would be circular — and create_provider verifies the
# instance reports this exact name, so a drift from the adapter's
# PROVIDER_NAME fails loudly instead of silently.


def _native_text_factory() -> Any:
    from soa_worker.native_text_adapter import PdfiumNativeTextProvider

    return PdfiumNativeTextProvider()


register_provider(
    ProviderInfo(
        name="pdfium-native-text",
        capability=Capability.NATIVE_TEXT,
        languages=(ANY_LANGUAGE,),
        data_policy=LOCAL_DATA_POLICY,
    ),
    _native_text_factory,
)
