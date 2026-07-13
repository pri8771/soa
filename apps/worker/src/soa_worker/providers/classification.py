"""Classifier and splitter provider contracts (AIO-001).

A classifier assigns a document ONE label from the closed set the
request carries (the stream's configured document types) — or abstains.
A splitter partitions a multi-document file into contiguous page ranges,
each optionally labelled from the same closed set.

Honesty rules:

- the label vocabulary is CLOSED: a result label outside
  ``request.labels`` is a contract violation, and abstention
  (``label=None``) carries confidence 0.0 — abstaining is never
  confident;
- split proposals must partition the request pages exactly (no gaps, no
  overlaps): a split that silently drops a page drops customer data;
- vendor DTOs never cross this interface; failures raise
  :class:`ClassificationProviderError` with a display-safe message.

Pages reuse the extraction contract's :class:`PageInput` (PRC-005
raster dimensions plus optional prior-stage text).
"""

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from soa_worker.extraction.provider import PageInput


def _validate_labels(labels: tuple[str, ...]) -> None:
    if not labels:
        raise ValueError("a request must offer at least one label")
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate labels in request")
    if any(not label.strip() for label in labels):
        raise ValueError("labels must be non-empty")


def _validate_pages(pages: tuple[PageInput, ...]) -> None:
    if not pages:
        raise ValueError("a request must carry at least one page")
    numbers = sorted(page.page_number for page in pages)
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        raise ValueError("request pages must be contiguous and unique")


@dataclass(frozen=True)
class ClassificationRequest:
    document_id: uuid.UUID
    document_sha256: str
    pages: tuple[PageInput, ...]
    #: The closed label vocabulary (the stream's configured types).
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_pages(self.pages)
        _validate_labels(self.labels)


@dataclass(frozen=True)
class ClassificationResult:
    provider: str
    #: One of the request labels, or None to abstain — never a guess
    #: outside the vocabulary.
    label: str | None
    confidence: float
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        if self.label is None and self.confidence != 0.0:
            raise ValueError("abstention carries confidence 0.0 — abstaining is never confident")
        if self.label is not None and not self.label.strip():
            raise ValueError("a label cannot be blank — abstain with None instead")


@dataclass(frozen=True)
class SplitRequest:
    document_id: uuid.UUID
    document_sha256: str
    pages: tuple[PageInput, ...]
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_pages(self.pages)
        _validate_labels(self.labels)


@dataclass(frozen=True)
class SplitProposal:
    """One contiguous page range the splitter believes is a document."""

    start_page: int  # 1-based, inclusive
    end_page: int  # inclusive
    label: str | None
    confidence: float

    def __post_init__(self) -> None:
        if self.start_page < 1:
            raise ValueError("page numbering is 1-based")
        if self.end_page < self.start_page:
            raise ValueError("a proposal cannot end before it starts")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class SplitResult:
    provider: str
    proposals: tuple[SplitProposal, ...]
    warnings: tuple[str, ...] = ()


class ClassificationProviderError(Exception):
    """Classification or splitting failed. ``retryable`` mirrors
    StageExecutionError classification; the message must be
    display-safe."""

    def __init__(self, safe_message: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(safe_message)


@runtime_checkable
class ClassifierProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier recorded on StageRun.provider."""
        ...

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Classify the document, or abstain. Must be deterministic for
        a given request."""
        ...


@runtime_checkable
class SplitterProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable provider identifier recorded on StageRun.provider."""
        ...

    async def split(self, request: SplitRequest) -> SplitResult:
        """Partition the request pages into proposals. Must be
        deterministic for a given request."""
        ...


def validate_classification_result(
    request: ClassificationRequest, result: ClassificationResult
) -> list[str]:
    """Contract checks shared by the test suite and distrustful callers."""
    violations: list[str] = []
    if not result.provider.strip():
        violations.append("result does not name its provider")
    if result.label is not None and result.label not in request.labels:
        violations.append(f"label {result.label!r} is outside the request vocabulary")
    return violations


def validate_split_result(request: SplitRequest, result: SplitResult) -> list[str]:
    """Contract checks: proposals must exactly partition the request
    pages, in order, and labels must stay inside the vocabulary."""
    violations: list[str] = []
    if not result.provider.strip():
        violations.append("result does not name its provider")
    for proposal in result.proposals:
        if proposal.label is not None and proposal.label not in request.labels:
            violations.append(f"label {proposal.label!r} is outside the request vocabulary")

    page_numbers = sorted(page.page_number for page in request.pages)
    expected_start = page_numbers[0]
    for proposal in result.proposals:
        if proposal.start_page != expected_start:
            violations.append(
                f"proposal starting at page {proposal.start_page} leaves a gap or overlap "
                f"(expected {expected_start}) — every page must belong to exactly one proposal"
            )
            break
        expected_start = proposal.end_page + 1
    else:
        last_page = page_numbers[-1]
        if result.proposals and expected_start != last_page + 1:
            violations.append(
                f"proposals end at page {expected_start - 1} but the document has {last_page}"
            )
        if not result.proposals:
            violations.append("a split result must cover the document with at least one proposal")
    return violations


__all__ = [
    "ClassificationProviderError",
    "ClassificationRequest",
    "ClassificationResult",
    "ClassifierProvider",
    "SplitProposal",
    "SplitRequest",
    "SplitResult",
    "SplitterProvider",
    "validate_classification_result",
    "validate_split_result",
]
