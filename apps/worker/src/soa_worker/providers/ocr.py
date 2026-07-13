"""OCR / layout provider contract (AIO-001; AIO-004/005/006 implement).

An OCR provider recognises text on rendered page images and returns a
word → line → block hierarchy with coordinates (PRC-005 system: top-left
origin, pixels of the request page's raster) and per-item confidence.

Honesty rules:

- results only name request pages, and the languages a result reports
  are a subset of the languages the request allowed — a provider must
  not silently recognise in a language the stream did not configure;
- confidence is the provider's own signal in [0, 1], never rescaled to
  look better;
- vendor DTOs never cross this interface; failures raise
  :class:`OcrProviderError` with a display-safe message.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class OcrPageInput:
    """One rendered page image to recognise (PRC-005 raster)."""

    page_number: int  # 1-based
    width_px: int
    height_px: int
    image: bytes
    content_type: str  # e.g. image/png

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page numbering is 1-based")
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("page dimensions must be positive")
        if not self.image:
            raise ValueError("page carries no image bytes")


@dataclass(frozen=True)
class OcrRequest:
    document_id: uuid.UUID
    document_sha256: str
    pages: tuple[OcrPageInput, ...]
    #: Lowercase language tags the stream allows, in priority order.
    languages: tuple[str, ...]

    def __post_init__(self) -> None:
        numbers = [page.page_number for page in self.pages]
        if len(set(numbers)) != len(numbers):
            raise ValueError("duplicate page numbers in request")
        if not self.languages:
            raise ValueError("a request must allow at least one language")
        for tag in self.languages:
            if not tag or tag != tag.strip().lower():
                raise ValueError(f"language tag {tag!r} must be a non-empty lowercase tag")


@dataclass(frozen=True)
class OcrWord:
    text: str
    polygon: tuple[tuple[float, float], ...]
    confidence: float

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("a word cannot be empty")
        if len(self.polygon) < 3:
            raise ValueError("word polygons need at least 3 vertices")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: tuple[OcrWord, ...]
    polygon: tuple[tuple[float, float], ...]
    confidence: float

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("a line cannot be empty")
        if len(self.polygon) < 3:
            raise ValueError("line polygons need at least 3 vertices")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class OcrBlock:
    lines: tuple[OcrLine, ...]
    polygon: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.polygon) < 3:
            raise ValueError("block polygons need at least 3 vertices")


@dataclass(frozen=True)
class OcrPageResult:
    page_number: int
    blocks: tuple[OcrBlock, ...]
    #: Languages the provider actually applied on this page.
    languages: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page numbering is 1-based")


@dataclass(frozen=True)
class OcrResult:
    provider: str
    pages: tuple[OcrPageResult, ...]
    model: str | None = None
    cost_cents: int = 0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cost_cents < 0:
            raise ValueError("cost cannot be negative")
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("pages must be unique and in ascending order")


class OcrProviderError(Exception):
    """OCR failed. ``retryable`` mirrors StageExecutionError
    classification; the message must be display-safe."""

    def __init__(self, safe_message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(safe_message)


@runtime_checkable
class OcrProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier recorded on StageRun.provider."""
        ...

    async def recognize(self, request: OcrRequest) -> OcrResult:
        """Recognise text on the request pages. Must be deterministic
        for a given request."""
        ...


def _polygons_of(block: OcrBlock) -> list[tuple[str, tuple[tuple[float, float], ...]]]:
    named = [("block", block.polygon)]
    for line in block.lines:
        named.append((f"line {line.text[:20]!r}", line.polygon))
        for word in line.words:
            named.append((f"word {word.text[:20]!r}", word.polygon))
    return named


def validate_ocr_result(request: OcrRequest, result: OcrResult) -> list[str]:
    """Contract checks shared by the test suite and distrustful callers.
    Returns human-readable violations (empty = conformant)."""
    violations: list[str] = []
    if not result.provider.strip():
        violations.append("result does not name its provider")
    allowed = set(request.languages)
    request_pages = {page.page_number: page for page in request.pages}
    for page_result in result.pages:
        page = request_pages.get(page_result.page_number)
        if page is None:
            violations.append(f"result names unknown page {page_result.page_number}")
            continue
        for tag in page_result.languages:
            if tag not in allowed:
                violations.append(
                    f"page {page.page_number} used language {tag!r} the request did not allow"
                )
        for block in page_result.blocks:
            for label, polygon in _polygons_of(block):
                for x, y in polygon:
                    if not (0 <= x <= page.width_px and 0 <= y <= page.height_px):
                        violations.append(
                            f"{label} leaves page {page.page_number} bounds at ({x}, {y})"
                        )
                        break
    return violations


__all__ = [
    "OcrBlock",
    "OcrLine",
    "OcrPageInput",
    "OcrPageResult",
    "OcrProvider",
    "OcrProviderError",
    "OcrRequest",
    "OcrResult",
    "OcrWord",
    "validate_ocr_result",
]
