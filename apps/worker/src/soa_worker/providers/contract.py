"""Reusable provider contract suites (AIO-001).

One suite per interface, mirroring the extraction pattern (PRC-006's
``ExtractionProviderContract``): each adapter's test module subclasses
the suite for its capability, implements ``make_provider`` plus the
fixture hooks, and passes unchanged. The suites encode the behaviours
downstream stages rely on — determinism across independent instances,
result conformance to the request, honest failure classification, no
fabrication — so an adapter that cannot pass one is not a provider of
that capability, whatever its vendor benchmarks say.

Shared fixture builders live here too (``synthetic_text_pages``,
``classification_fixture``, ``split_fixture``, ``GIBBERISH_TEXT``) so
classifier/splitter adapters exercise the SAME inputs; native-text and
OCR fixtures are necessarily adapter-supplied bytes (a real PDF, a real
raster), which is why those hooks are abstract.
"""

import uuid

from soa_worker.extraction.provider import PageInput
from soa_worker.providers.classification import (
    ClassificationRequest,
    ClassifierProvider,
    SplitRequest,
    SplitterProvider,
    validate_classification_result,
    validate_split_result,
)
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextFailure,
    NativeTextProvider,
    NativeTextRequest,
    validate_native_text_result,
)
from soa_worker.providers.ocr import OcrProvider, OcrRequest, validate_ocr_result

#: Deliberately meaningless page text no classifier should match to any
#: real document-type label — the shared abstention fixture.
GIBBERISH_TEXT = "zzqx vlorp 000000 ~~~~ unmatched nonsense corpus entry"

#: The default label vocabulary shared fixtures use.
FIXTURE_LABELS = ("purchase_order", "invoice", "other")

_FIXTURE_NAMESPACE = uuid.UUID("2f0a35b8-6f43-46a1-9f7e-52a1c1a70001")


def synthetic_text_pages(texts: tuple[str, ...]) -> tuple[PageInput, ...]:
    """Contiguous 1-based pages carrying the given texts on a standard
    A4-ish raster."""
    return tuple(
        PageInput(page_number=index + 1, width_px=1240, height_px=1754, text=text)
        for index, text in enumerate(texts)
    )


def classification_fixture(texts: tuple[str, ...]) -> ClassificationRequest:
    return ClassificationRequest(
        document_id=uuid.uuid5(_FIXTURE_NAMESPACE, "classify:" + "|".join(texts)),
        document_sha256="0" * 64,
        pages=synthetic_text_pages(texts),
        labels=FIXTURE_LABELS,
    )


def split_fixture(texts: tuple[str, ...]) -> SplitRequest:
    return SplitRequest(
        document_id=uuid.uuid5(_FIXTURE_NAMESPACE, "split:" + "|".join(texts)),
        document_sha256="0" * 64,
        pages=synthetic_text_pages(texts),
        labels=FIXTURE_LABELS,
    )


class NativeTextProviderContract:
    """Subclass per adapter; override make_provider(), contract_request()
    and rejection_fixtures()."""

    async def make_provider(self) -> NativeTextProvider:
        raise NotImplementedError

    def contract_request(self) -> NativeTextRequest:
        """A document this adapter CAN read at least one span from."""
        raise NotImplementedError

    def rejection_fixtures(self) -> dict[NativeTextFailure, NativeTextRequest]:
        """Documents the adapter must REFUSE, keyed by the failure kind
        it must classify them as (encrypted / corrupt / ...). AIO-002
        acceptance requires these; an adapter without any skips visibly."""
        return {}

    async def test_is_deterministic_across_independent_instances(self) -> None:
        request = self.contract_request()
        first = await (await self.make_provider()).read(request)
        second = await (await self.make_provider()).read(request)
        assert first == second, "two runs over the same request must be identical"

    async def test_reads_at_least_one_span_from_the_contract_fixture(self) -> None:
        result = await (await self.make_provider()).read(self.contract_request())
        assert any(page.spans for page in result.pages), (
            "the contract fixture must yield at least one text span — "
            "an empty result proves nothing about the adapter"
        )

    async def test_result_conforms_to_the_request(self) -> None:
        request = self.contract_request()
        result = await (await self.make_provider()).read(request)
        assert validate_native_text_result(request, result) == []

    async def test_result_names_the_provider(self) -> None:
        provider = await self.make_provider()
        result = await provider.read(self.contract_request())
        assert result.provider == provider.name

    async def test_unreadable_documents_are_classified_not_half_read(self) -> None:
        import pytest  # test-only dependency; the suite runs under pytest

        fixtures = self.rejection_fixtures()
        if not fixtures:
            pytest.skip("adapter supplied no rejection fixtures (required from AIO-002 on)")
        provider = await self.make_provider()
        for expected_failure, request in fixtures.items():
            with pytest.raises(NativeTextError) as caught:
                await provider.read(request)
            assert caught.value.failure == expected_failure
            assert str(caught.value).strip(), "the refusal must say why (display-safe)"


class OcrProviderContract:
    """Subclass per adapter; override make_provider() and contract_request()."""

    async def make_provider(self) -> OcrProvider:
        raise NotImplementedError

    def contract_request(self) -> OcrRequest:
        """A page image this adapter can recognise at least one word on."""
        raise NotImplementedError

    async def test_is_deterministic_across_independent_instances(self) -> None:
        request = self.contract_request()
        first = await (await self.make_provider()).recognize(request)
        second = await (await self.make_provider()).recognize(request)
        assert first == second, "two runs over the same request must be identical"

    async def test_recognises_at_least_one_word_from_the_contract_fixture(self) -> None:
        result = await (await self.make_provider()).recognize(self.contract_request())
        words = [
            word
            for page in result.pages
            for block in page.blocks
            for line in block.lines
            for word in line.words
        ]
        assert words, "the contract fixture must yield at least one recognised word"

    async def test_result_conforms_to_the_request(self) -> None:
        request = self.contract_request()
        result = await (await self.make_provider()).recognize(request)
        assert validate_ocr_result(request, result) == []

    async def test_result_names_the_provider(self) -> None:
        provider = await self.make_provider()
        result = await provider.recognize(self.contract_request())
        assert result.provider == provider.name


class ClassifierProviderContract:
    """Subclass per adapter; override make_provider(). The fixtures are
    shared unless the adapter needs its own (override the hooks)."""

    async def make_provider(self) -> ClassifierProvider:
        raise NotImplementedError

    def contract_request(self) -> ClassificationRequest:
        """A request the adapter classifies to a NON-None label."""
        raise NotImplementedError

    def abstain_request(self) -> ClassificationRequest:
        """Content matching no label: the adapter must abstain, never
        guess. The shared gibberish fixture by default."""
        return classification_fixture((GIBBERISH_TEXT,))

    async def test_is_deterministic_across_independent_instances(self) -> None:
        request = self.contract_request()
        first = await (await self.make_provider()).classify(request)
        second = await (await self.make_provider()).classify(request)
        assert first == second, "two runs over the same request must be identical"

    async def test_classifies_the_contract_fixture(self) -> None:
        request = self.contract_request()
        result = await (await self.make_provider()).classify(request)
        assert result.label is not None, "the contract fixture must classify to a label"
        assert validate_classification_result(request, result) == []

    async def test_abstains_on_unmatchable_content(self) -> None:
        result = await (await self.make_provider()).classify(self.abstain_request())
        assert result.label is None, (
            "content matching no label must yield abstention, never a fabricated label"
        )

    async def test_result_names_the_provider(self) -> None:
        provider = await self.make_provider()
        result = await provider.classify(self.contract_request())
        assert result.provider == provider.name


class SplitterProviderContract:
    """Subclass per adapter; override make_provider()."""

    async def make_provider(self) -> SplitterProvider:
        raise NotImplementedError

    def contract_request(self) -> SplitRequest:
        """A multi-page request the adapter can partition."""
        raise NotImplementedError

    async def test_is_deterministic_across_independent_instances(self) -> None:
        request = self.contract_request()
        first = await (await self.make_provider()).split(request)
        second = await (await self.make_provider()).split(request)
        assert first == second, "two runs over the same request must be identical"

    async def test_proposals_partition_the_document_exactly(self) -> None:
        request = self.contract_request()
        result = await (await self.make_provider()).split(request)
        assert validate_split_result(request, result) == []

    async def test_result_names_the_provider(self) -> None:
        provider = await self.make_provider()
        result = await provider.split(self.contract_request())
        assert result.provider == provider.name
