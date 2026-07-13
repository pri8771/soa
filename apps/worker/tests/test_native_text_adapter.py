"""Native PDF text adapter tests (AIO-002): the shared contract suite
over the digital corpus, coordinate parity with the viewer raster,
coverage/quality honesty, and classified refusals for encrypted /
corrupt / missing-font documents."""

import sys
import uuid
from hashlib import sha256
from pathlib import Path

import pytest

import soa_worker.native_text_adapter as adapter_module
from soa_worker.native_text_adapter import PROVIDER_NAME, PdfiumNativeTextProvider
from soa_worker.providers import LOCAL_DATA_POLICY, Capability, provider_info
from soa_worker.providers.contract import NativeTextProviderContract
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextFailure,
    NativeTextProvider,
    NativeTextRequest,
    NativeTextResult,
    validate_native_text_result,
)
from soa_worker.rendering import RenderLimits, render_document

CORPUS = Path(__file__).parent / "fixtures" / "pdfs"
DOC_ID = uuid.UUID("5b7a1c00-0000-4000-8000-0000000000aa")


def corpus_request(
    name: str, *, content_type: str = "application/pdf", max_pages: int = 500
) -> NativeTextRequest:
    data = (CORPUS / name).read_bytes()
    return NativeTextRequest(
        document_id=DOC_ID,
        document_sha256=sha256(data).hexdigest(),
        content_type=content_type,
        data=data,
        max_pages=max_pages,
    )


async def read(name: str, **kwargs: object) -> NativeTextResult:
    return await PdfiumNativeTextProvider().read(corpus_request(name, **kwargs))  # type: ignore[arg-type]


def span_texts(result: NativeTextResult, page_number: int) -> list[str]:
    (page,) = [p for p in result.pages if p.page_number == page_number]
    return [span.text for span in page.spans]


class TestPdfiumNativeTextContract(NativeTextProviderContract):
    async def make_provider(self) -> NativeTextProvider:
        return PdfiumNativeTextProvider()

    def contract_request(self) -> NativeTextRequest:
        return corpus_request("digital-po.pdf")

    def rejection_fixtures(self) -> dict[NativeTextFailure, NativeTextRequest]:
        return {
            NativeTextFailure.ENCRYPTED: corpus_request("encrypted.pdf"),
            NativeTextFailure.CORRUPT: corpus_request("corrupt.pdf"),
        }


class TestDigitalCorpus:
    async def test_reads_both_pages_with_full_coverage(self) -> None:
        result = await read("digital-po.pdf")
        assert [page.page_number for page in result.pages] == [1, 2]
        assert all(page.coverage == 1.0 for page in result.pages)
        assert "PURCHASE ORDER PO-4711" in span_texts(result, 1)
        assert "Total: 62.50" in span_texts(result, 2)
        assert result.warnings == ()

    async def test_coordinates_match_the_viewer_raster(self) -> None:
        """The acceptance: text coordinates live in the SAME pixel space
        as the rendered page the viewer shows — identical dimensions
        from identical limits, and the known title lands where the page
        drew it (top-left region, 72pt indent)."""
        request = corpus_request("digital-po.pdf")
        result = await PdfiumNativeTextProvider().read(request)
        rendered = await render_document(request.data, content_type="application/pdf")
        assert [(p.page_number, p.width_px, p.height_px) for p in result.pages] == [
            (p.page_number, p.width_px, p.height_px) for p in rendered
        ]

        (page_one,) = [p for p in result.pages if p.page_number == 1]
        (title,) = [s for s in page_one.spans if s.text == "PURCHASE ORDER PO-4711"]
        xs = [x for x, _ in title.polygon]
        ys = [y for _, y in title.polygon]
        # 612x792pt page at dpi 200 → scale 2.78: x starts near 72pt≈200px,
        # the title sits in the top tenth of the page.
        assert 180 < min(xs) < 230
        assert max(ys) < 0.12 * page_one.height_px

    async def test_page_cap_truncates_with_a_warning(self) -> None:
        request = corpus_request("digital-po.pdf", max_pages=1)
        result = await PdfiumNativeTextProvider().read(request)
        assert [page.page_number for page in result.pages] == [1]
        assert any("first 1" in warning for warning in result.warnings)
        assert validate_native_text_result(request, result) == []

    async def test_a_blank_page_reports_zero_coverage_not_fake_text(self) -> None:
        result = await read("blank-page.pdf")
        (page,) = result.pages
        assert page.spans == ()
        assert page.coverage == 0.0
        assert any("no native text" in warning for warning in result.warnings)


class TestClassifiedRefusals:
    async def test_encrypted_documents_are_classified_terminal(self) -> None:
        with pytest.raises(NativeTextError) as caught:
            await read("encrypted.pdf")
        assert caught.value.failure is NativeTextFailure.ENCRYPTED
        assert caught.value.retryable is False
        assert "password" in str(caught.value)

    async def test_corrupt_documents_are_classified_terminal(self) -> None:
        with pytest.raises(NativeTextError) as caught:
            await read("corrupt.pdf")
        assert caught.value.failure is NativeTextFailure.CORRUPT
        assert caught.value.retryable is False

    async def test_unmappable_fonts_are_refused_not_returned_as_garbage(self) -> None:
        with pytest.raises(NativeTextError) as caught:
            await read("missing-font.pdf")
        assert caught.value.failure is NativeTextFailure.FONTS_UNMAPPABLE
        assert caught.value.retryable is False
        assert "OCR" in str(caught.value)

    async def test_non_pdf_content_types_are_unsupported(self) -> None:
        with pytest.raises(NativeTextError) as caught:
            await read("digital-po.pdf", content_type="image/png")
        assert caught.value.failure is NativeTextFailure.UNSUPPORTED


class TestOperationalFailures:
    async def test_timeout_kills_the_sandbox_and_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_module,
            "_SANDBOX_ARGV",
            [sys.executable, "-c", "import time; time.sleep(600)"],
        )
        provider = PdfiumNativeTextProvider(limits=RenderLimits(timeout_seconds=1.0))
        with pytest.raises(NativeTextError) as caught:
            await provider.read(corpus_request("digital-po.pdf"))
        assert caught.value.failure is NativeTextFailure.UNAVAILABLE
        assert caught.value.retryable is True

    async def test_a_crashed_child_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            adapter_module,
            "_SANDBOX_ARGV",
            [sys.executable, "-c", "import sys; sys.exit(9)"],
        )
        with pytest.raises(NativeTextError) as caught:
            await PdfiumNativeTextProvider().read(corpus_request("digital-po.pdf"))
        assert caught.value.failure is NativeTextFailure.UNAVAILABLE
        assert caught.value.retryable is True


def test_registered_as_a_local_native_text_capability() -> None:
    registered = provider_info(Capability.NATIVE_TEXT, PROVIDER_NAME)
    assert registered.data_policy == LOCAL_DATA_POLICY
    assert registered.supports_language("de")
