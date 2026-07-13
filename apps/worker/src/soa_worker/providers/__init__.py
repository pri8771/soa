"""Document-AI provider contracts and the capability registry (AIO-001).

The interfaces the AIO epic's adapters implement, alongside the
extraction contract that already lives in ``soa_worker.extraction``
(PRC-006):

- :mod:`soa_worker.providers.native_text` — digital text with spans,
  coordinates, coverage (AIO-002 implements);
- :mod:`soa_worker.providers.ocr` — word/line/block recognition with
  coordinates and confidence (AIO-004/005/006 implement);
- :mod:`soa_worker.providers.classification` — closed-vocabulary
  classifier and page-range splitter;
- :mod:`soa_worker.providers.capabilities` — the registry carrying
  capability / language / region / data-policy metadata, with
  fail-closed selection;
- :mod:`soa_worker.providers.contract` — the shared contract suites and
  fixtures every adapter's tests reuse (import it from tests, not here).

Vendor DTOs never cross these interfaces: requests and results are the
plain dataclasses defined here, and anything vendor-shaped stays inside
the adapter that talks to that vendor.
"""

import soa_worker.providers.builtin  # noqa: F401  (registers built-in adapters)
from soa_worker.providers.capabilities import (
    ANY_LANGUAGE,
    LOCAL_DATA_POLICY,
    Capability,
    DataPolicy,
    NoCapableProviderError,
    ProviderInfo,
    UnknownProviderError,
    create_provider,
    provider_info,
    register_provider,
    registered_providers,
    require_provider,
    select_providers,
    unregister_provider,
)

__all__ = [
    "ANY_LANGUAGE",
    "LOCAL_DATA_POLICY",
    "Capability",
    "DataPolicy",
    "NoCapableProviderError",
    "ProviderInfo",
    "UnknownProviderError",
    "create_provider",
    "provider_info",
    "register_provider",
    "registered_providers",
    "require_provider",
    "select_providers",
    "unregister_provider",
]
