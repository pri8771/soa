"""Native (digital) text provider contract (AIO-001; AIO-002 implements).

A native-text provider reads the text a digital document already
carries — no OCR, no model. Results are page-anchored spans in the
PRC-005 coordinate system (top-left origin, pixels of the raster whose
dimensions the result page declares), so the viewer can overlay them
without translation.

Honesty rules:

- a document the provider cannot read raises :class:`NativeTextError`
  with a CLASSIFIED failure kind (encrypted / corrupt / fonts
  unmappable / unsupported) and a display-safe message — never a
  half-empty "success";
- ``coverage`` is the provider's own estimate of how much of a page's
  text it captured, in [0, 1] — a page with unmappable glyphs must not
  report 1.0;
- results never exceed ``max_pages``; truncation adds a warning.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class NativeTextFailure(StrEnum):
    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    #: The file opens but its fonts cannot be mapped to text (scanned
    #: wrappers, subset fonts without a unicode map).
    FONTS_UNMAPPABLE = "fonts_unmappable"
    UNSUPPORTED = "unsupported"


class NativeTextError(Exception):
    """Native text extraction failed. The message must be display-safe
    (no vendor payloads, no document content)."""

    def __init__(
        self, safe_message: str, *, failure: NativeTextFailure, retryable: bool = False
    ) -> None:
        self.failure = failure
        self.retryable = retryable
        super().__init__(safe_message)


@dataclass(frozen=True)
class NativeTextRequest:
    document_id: uuid.UUID
    #: SHA-256 (hex) of the ORIGINAL document bytes.
    document_sha256: str
    content_type: str
    #: The original document bytes — native text reads the source file,
    #: not a rendered raster.
    data: bytes
    max_pages: int = 500

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("request carries no document bytes")
        if self.max_pages < 1:
            raise ValueError("max_pages must be at least 1")


@dataclass(frozen=True)
class TextSpan:
    """One run of text at one place on a page."""

    text: str
    #: [x, y] vertices in the page's declared pixel space; at least 3.
    polygon: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("a text span cannot be empty")
        if len(self.polygon) < 3:
            raise ValueError("span polygons need at least 3 vertices")


@dataclass(frozen=True)
class NativeTextPage:
    page_number: int  # 1-based
    width_px: int
    height_px: int
    spans: tuple[TextSpan, ...]
    #: The provider's estimate of the fraction of this page's text it
    #: captured, in [0, 1].
    coverage: float

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page numbering is 1-based")
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("page dimensions must be positive")
        if not 0.0 <= self.coverage <= 1.0:
            raise ValueError("coverage must be within [0, 1]")
        for span in self.spans:
            for x, y in span.polygon:
                if not (0 <= x <= self.width_px and 0 <= y <= self.height_px):
                    raise ValueError(
                        f"span {span.text[:20]!r} leaves page {self.page_number} "
                        f"bounds at ({x}, {y})"
                    )


@dataclass(frozen=True)
class NativeTextResult:
    provider: str
    pages: tuple[NativeTextPage, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("pages must be unique and in ascending order")


@runtime_checkable
class NativeTextProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier recorded on StageRun.provider."""
        ...

    async def read(self, request: NativeTextRequest) -> NativeTextResult:
        """Read the document's native text. Must be deterministic for a
        given request."""
        ...


def validate_native_text_result(request: NativeTextRequest, result: NativeTextResult) -> list[str]:
    """Contract checks shared by the test suite and distrustful callers.
    Returns human-readable violations (empty = conformant)."""
    violations: list[str] = []
    if not result.provider.strip():
        violations.append("result does not name its provider")
    if len(result.pages) > request.max_pages:
        violations.append(
            f"result has {len(result.pages)} pages but the request capped at {request.max_pages}"
        )
    return violations


__all__ = [
    "NativeTextError",
    "NativeTextFailure",
    "NativeTextPage",
    "NativeTextProvider",
    "NativeTextRequest",
    "NativeTextResult",
    "TextSpan",
    "validate_native_text_result",
]
