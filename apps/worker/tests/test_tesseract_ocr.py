"""Tesseract OCR adapter tests (AIO-004): the shared contract suite on
a rendered fixture, coordinates and confidence, fail-closed languages,
resource limits, and conditional registration. The whole module skips
visibly when the engine is not installed (CI installs it)."""

import io
import shutil
import uuid

import pytest
from PIL import Image, ImageDraw, ImageFont

from soa_worker.providers import LOCAL_DATA_POLICY, Capability, provider_info
from soa_worker.providers.contract import OcrProviderContract
from soa_worker.providers.ocr import (
    OcrPageInput,
    OcrProvider,
    OcrProviderError,
    OcrRequest,
    OcrResult,
    validate_ocr_result,
)
from soa_worker.tesseract_ocr import (
    PROVIDER_NAME,
    OcrLimits,
    TesseractOcrProvider,
    installed_language_tags,
    tesseract_available,
)

pytestmark = pytest.mark.skipif(
    not tesseract_available(), reason="tesseract binary not installed (CI installs it)"
)

DOC_ID = uuid.UUID("9d2e6f00-0000-4000-8000-0000000000cc")
WIDTH, HEIGHT = 800, 300


def rendered_page(
    lines: tuple[str, ...] = ("PURCHASE ORDER PO-4711", "Total: 62.50 EUR"),
    *,
    page_number: int = 1,
) -> OcrPageInput:
    image = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default(36)  # type: ignore[assignment]
    for index, text in enumerate(lines):
        draw.text((40, 40 + 80 * index), text, fill=(0, 0, 0), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return OcrPageInput(
        page_number=page_number,
        width_px=WIDTH,
        height_px=HEIGHT,
        image=buffer.getvalue(),
        content_type="image/png",
    )


def request(
    pages: tuple[OcrPageInput, ...] | None = None,
    languages: tuple[str, ...] = ("en",),
) -> OcrRequest:
    return OcrRequest(
        document_id=DOC_ID,
        document_sha256="c" * 64,
        pages=pages if pages is not None else (rendered_page(),),
        languages=languages,
    )


def all_words(result: OcrResult) -> list[str]:
    return [
        word.text
        for page in result.pages
        for block in page.blocks
        for line in block.lines
        for word in line.words
    ]


class TestTesseractContract(OcrProviderContract):
    async def make_provider(self) -> OcrProvider:
        return TesseractOcrProvider()

    def contract_request(self) -> OcrRequest:
        return request()


class TestRecognition:
    async def test_reads_the_rendered_text_with_geometry_and_confidence(self) -> None:
        result = await TesseractOcrProvider().recognize(request())
        words = all_words(result)
        assert {"PURCHASE", "ORDER", "PO-4711", "62.50"} <= set(words)
        (page,) = result.pages
        first_word = page.blocks[0].lines[0].words[0]
        assert first_word.text == "PURCHASE"
        xs = [x for x, _ in first_word.polygon]
        ys = [y for _, y in first_word.polygon]
        # Drawn at (40, 40) in a 36pt font: the box starts near there.
        assert 25 <= min(xs) <= 70
        assert 25 <= min(ys) <= 70
        assert first_word.confidence > 0.5
        assert validate_ocr_result(request(), result) == []

    async def test_lines_and_blocks_group_the_words(self) -> None:
        result = await TesseractOcrProvider().recognize(request())
        (page,) = result.pages
        line_texts = [line.text for block in page.blocks for line in block.lines]
        assert "PURCHASE ORDER PO-4711" in line_texts
        assert any("62.50" in text for text in line_texts)

    async def test_multiple_pages_come_back_in_order(self) -> None:
        pages = (
            rendered_page(("Page one text",), page_number=1),
            rendered_page(("Page two text",), page_number=2),
        )
        result = await TesseractOcrProvider().recognize(request(pages=pages))
        assert [page.page_number for page in result.pages] == [1, 2]

    async def test_a_blank_page_yields_no_words_not_fabrications(self) -> None:
        blank = rendered_page(())
        result = await TesseractOcrProvider().recognize(request(pages=(blank,)))
        (page,) = result.pages
        assert page.blocks == ()

    async def test_the_result_names_the_engine_version(self) -> None:
        result = await TesseractOcrProvider().recognize(request())
        assert result.model is not None and "tesseract" in result.model


class TestLanguages:
    async def test_german_pack_recognises_german_text(self) -> None:
        if "de" not in installed_language_tags():
            pytest.skip("deu language pack not installed")
        page = rendered_page(("Rechnung Nummer 42",))
        result = await TesseractOcrProvider().recognize(request(pages=(page,), languages=("de",)))
        assert "Rechnung" in all_words(result)
        (page_result,) = result.pages
        assert page_result.languages == ("de",)

    async def test_unknown_language_tags_are_refused(self) -> None:
        with pytest.raises(OcrProviderError, match="not supported"):
            await TesseractOcrProvider().recognize(request(languages=("xx",)))

    async def test_uninstalled_packs_are_refused_naming_what_is_installed(self) -> None:
        if "pl" in installed_language_tags():
            pytest.skip("polish pack unexpectedly installed")
        with pytest.raises(OcrProviderError, match="not installed") as caught:
            await TesseractOcrProvider().recognize(request(languages=("pl",)))
        assert caught.value.retryable is False


class TestLimits:
    async def test_timeout_kills_the_engine_and_is_retryable(self) -> None:
        provider = TesseractOcrProvider(limits=OcrLimits(timeout_seconds=0.02))
        with pytest.raises(OcrProviderError, match="budget") as caught:
            await provider.recognize(request())
        assert caught.value.retryable is True


def test_registered_with_the_installed_languages_only() -> None:
    registered = provider_info(Capability.OCR, PROVIDER_NAME)
    assert registered.data_policy == LOCAL_DATA_POLICY
    assert registered.languages == installed_language_tags()
    assert "en" in registered.languages
    assert not shutil.which("tesseract") or registered.supports_language("en")
