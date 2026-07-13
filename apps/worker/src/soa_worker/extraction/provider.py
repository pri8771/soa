"""Extraction provider contract (PRC-006).

Every extraction adapter — the deterministic mock today, native-text /
OCR / hosted-model adapters with the AIO epic — implements exactly this
interface. Vendor DTOs never cross it: requests carry plain field specs
(flat schema keys and type names, matching ``SchemaDefinition.field_types``)
and results carry raw string values with page-anchored evidence in the
PRC-005 coordinate system (top-left origin, pixels of the stored raster).

Honesty rules encoded here and enforced by the contract suite:

- a field the provider could not find is returned with ``raw_value=None``
  and NO evidence — absence is explicit, never a fabricated value;
- evidence always names a request page and stays inside its pixel bounds;
- confidence is the provider's own signal in [0, 1] — downstream policy
  (PRC-011) treats it as one input, never the decision.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PageInput:
    """One rendered page the provider may extract from (PRC-005 raster)."""

    page_number: int  # 1-based, stable within the run
    width_px: int
    height_px: int
    #: Extracted text for the page when a prior stage produced it; None
    #: means the provider works from the image (or declines).
    text: str | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page numbering is 1-based")
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("page dimensions must be positive")


@dataclass(frozen=True)
class FieldSpec:
    """A schema field the caller wants extracted.

    ``key`` uses the flat form from ``SchemaDefinition.field_types``:
    header fields are bare keys, table columns are ``table.column``.
    ``field_type`` is the schema type name (text/number/money/date/
    boolean/enum/table) carried as a plain string so the worker never
    imports API domain models.
    """

    key: str
    field_type: str
    enum_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ExtractionRequest:
    document_id: uuid.UUID
    #: SHA-256 (hex) of the ORIGINAL document bytes — the stable identity
    #: providers may key fixtures or caches on.
    document_sha256: str
    content_type: str
    pages: tuple[PageInput, ...]
    fields: tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        numbers = [page.page_number for page in self.pages]
        if len(set(numbers)) != len(numbers):
            raise ValueError("duplicate page numbers in request")


@dataclass(frozen=True)
class EvidenceSpan:
    """Where a value came from: a polygon on a request page, plus the
    verbatim quote when the provider has one. Coordinates follow PRC-005:
    top-left origin, pixels of the raster whose dimensions the request's
    PageInput carries."""

    page_number: int
    #: [x, y] vertices; at least 3 for an area. Empty polygons are not
    #: allowed — page-level evidence uses the full page rectangle.
    polygon: tuple[tuple[float, float], ...]
    quote: str | None = None

    def __post_init__(self) -> None:
        if len(self.polygon) < 3:
            raise ValueError("evidence polygons need at least 3 vertices")


@dataclass(frozen=True)
class FieldCandidate:
    """An alternative reading the provider also considered."""

    raw_value: str
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class ExtractedField:
    field_key: str
    #: Verbatim value as read from the document; None = not found (and
    #: then evidence must be empty — absence is never evidenced).
    raw_value: str | None
    confidence: float
    #: Row within a table field, 0-based; None for header fields.
    row_index: int | None = None
    evidence: tuple[EvidenceSpan, ...] = ()
    candidates: tuple[FieldCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        if self.raw_value is None and self.evidence:
            raise ValueError("an absent value cannot carry evidence")


@dataclass(frozen=True)
class ExtractionResult:
    provider: str
    fields: tuple[ExtractedField, ...]
    model: str | None = None
    cost_cents: int = 0
    warnings: tuple[str, ...] = ()
    #: Exact instruction version (AIO-010 ``reference``) the call used;
    #: None for providers that take no instructions (the mock).
    instruction_reference: str | None = None


class ExtractionProviderError(Exception):
    """Extraction failed. ``retryable`` mirrors StageExecutionError
    classification; the message must be display-safe (no vendor payloads,
    no document content)."""

    def __init__(self, safe_message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(safe_message)


@runtime_checkable
class ExtractionProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier recorded on StageRun.provider."""
        ...

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Extract the requested fields. Must be deterministic for a
        given request unless the underlying model genuinely is not (the
        mock and local adapters are; hosted adapters document drift)."""
        ...


def requested_keys(request: ExtractionRequest) -> frozenset[str]:
    return frozenset(spec.key for spec in request.fields)


def validate_result_against_request(
    request: ExtractionRequest, result: ExtractionResult
) -> list[str]:
    """Contract-level checks shared by the test suite and any caller that
    wants to distrust an adapter at runtime. Returns human-readable
    violations (empty = conformant)."""
    violations: list[str] = []
    keys = requested_keys(request)
    pages = {page.page_number: page for page in request.pages}
    for extracted in result.fields:
        if extracted.field_key not in keys:
            violations.append(f"field {extracted.field_key!r} was not requested")
        for span in extracted.evidence:
            page = pages.get(span.page_number)
            if page is None:
                violations.append(
                    f"evidence for {extracted.field_key!r} names unknown page {span.page_number}"
                )
                continue
            for x, y in span.polygon:
                if not (0 <= x <= page.width_px and 0 <= y <= page.height_px):
                    violations.append(
                        f"evidence for {extracted.field_key!r} leaves page "
                        f"{span.page_number} bounds at ({x}, {y})"
                    )
                    break
    return violations


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
    "requested_keys",
    "validate_result_against_request",
]
