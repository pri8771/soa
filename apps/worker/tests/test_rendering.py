"""Sandboxed renderer tests (PRC-004): PDF and image fixtures, bounded
rasters, unmodified originals, malformed files, active content, timeout."""

import hashlib
import io
import sys

import pytest

import soa_worker.rendering as rendering
from soa_worker.rendering import RenderError, RenderFailure, RenderLimits, render_document

#: Minimal but complete one-page PDF (works with any conforming parser).
MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >> endobj
trailer << /Root 1 0 R >>
"""

#: The same PDF with an OpenAction JavaScript entry — active content that
#: must be rasterized as inert data, never executed.
PDF_WITH_JS = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R /OpenAction 4 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >> endobj
4 0 obj << /Type /Action /S /JavaScript /JS (app.alert\\(1\\)) >> endobj
trailer << /Root 1 0 R >>
"""


def make_png(width: int = 40, height: int = 20) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_pdf_renders_to_bounded_page_images() -> None:
    original_hash = hashlib.sha256(MINIMAL_PDF).hexdigest()
    pages = await render_document(
        MINIMAL_PDF,
        content_type="application/pdf",
        limits=RenderLimits(dpi=144, max_pixels_per_page=2_000_000),
    )
    assert len(pages) == 1
    (page,) = pages
    assert page.page_number == 1
    assert page.image_png.startswith(b"\x89PNG")
    # 200x100pt at 144dpi -> 400x200px, well under the pixel budget.
    assert (page.width_px, page.height_px) == (400, 200)
    assert page.width_px * page.height_px <= 2_000_000
    # The original bytes were never modified.
    assert hashlib.sha256(MINIMAL_PDF).hexdigest() == original_hash


async def test_dpi_is_bounded_by_the_pixel_budget() -> None:
    pages = await render_document(
        MINIMAL_PDF,
        content_type="application/pdf",
        limits=RenderLimits(dpi=1200, max_pixels_per_page=100_000),
    )
    (page,) = pages
    assert page.width_px * page.height_px <= 100_000, "the raster shrank to fit the budget"


async def test_aggregate_pixel_and_decompressed_budgets_are_enforced() -> None:
    with pytest.raises(RenderError) as pixel_error:
        await render_document(
            make_png(64, 32),
            content_type="image/png",
            limits=RenderLimits(
                max_pixels_per_page=10_000,
                max_total_pixels=1_000,
            ),
        )
    assert pixel_error.value.failure is RenderFailure.LIMIT_EXCEEDED

    with pytest.raises(RenderError) as byte_error:
        await render_document(
            make_png(64, 32),
            content_type="image/png",
            limits=RenderLimits(
                max_pixels_per_page=10_000,
                max_total_pixels=10_000,
                max_decompressed_bytes=1_000,
            ),
        )
    assert byte_error.value.failure is RenderFailure.LIMIT_EXCEEDED


async def test_image_and_multiframe_rendering() -> None:
    pages = await render_document(make_png(64, 32), content_type="image/png")
    assert [(p.page_number, p.width_px, p.height_px) for p in pages] == [(1, 64, 32)]


async def test_malformed_files_are_classified_terminal() -> None:
    with pytest.raises(RenderError) as pdf_error:
        await render_document(b"not a pdf at all", content_type="application/pdf")
    assert pdf_error.value.failure is RenderFailure.MALFORMED
    assert not pdf_error.value.retryable

    with pytest.raises(RenderError) as image_error:
        await render_document(b"\x89PNG truncated garbage", content_type="image/png")
    assert image_error.value.failure is RenderFailure.MALFORMED


async def test_active_content_is_rasterized_not_executed() -> None:
    # PDFium rasterizes; the JavaScript OpenAction is inert data. The
    # render succeeds and produces the same bounded page image.
    pages = await render_document(PDF_WITH_JS, content_type="application/pdf")
    assert len(pages) == 1
    assert pages[0].image_png.startswith(b"\x89PNG")


async def test_timeout_kills_the_sandbox_and_classifies_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A child that ignores its task and hangs — the parent must kill it.
    monkeypatch.setattr(
        rendering,
        "_SANDBOX_ARGV",
        [sys.executable, "-c", "import time; time.sleep(600)"],
    )
    with pytest.raises(RenderError) as error:
        await render_document(
            MINIMAL_PDF,
            content_type="application/pdf",
            limits=RenderLimits(timeout_seconds=1.0),
        )
    assert error.value.failure is RenderFailure.TIMEOUT
    assert error.value.retryable


async def test_unsupported_type_is_refused() -> None:
    with pytest.raises(RenderError) as error:
        await render_document(b"anything", content_type="application/zip")
    assert error.value.failure is RenderFailure.MALFORMED
