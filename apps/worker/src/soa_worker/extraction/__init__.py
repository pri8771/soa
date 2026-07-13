"""Extraction provider contract and adapters (PRC-006, AIO-001+).

The contract lives here in the worker because extraction happens in the
worker; adapters (mock now, native-text/OCR/hosted with the AIO epic)
implement :class:`~soa_worker.extraction.provider.ExtractionProvider`
and pass :class:`~soa_worker.extraction.contract.ExtractionProviderContract`.
"""

from soa_worker.extraction.provider import (
    EvidenceSpan,
    ExtractedField,
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    FieldCandidate,
    FieldSpec,
    PageInput,
)

__all__ = [
    "EvidenceSpan",
    "ExtractedField",
    "ExtractionProvider",
    "ExtractionProviderError",
    "ExtractionRequest",
    "ExtractionResult",
    "FieldCandidate",
    "FieldSpec",
    "PageInput",
]
