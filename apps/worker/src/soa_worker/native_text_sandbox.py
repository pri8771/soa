"""Sandboxed native-text extraction child process (AIO-002).

Runs as ``python -m soa_worker.native_text_sandbox <input> <max_pages>
<max_pixels_per_page> <dpi> <cpu_seconds> <memory_bytes>`` and NEVER in
the worker process — parsing an untrusted PDF gets the same lockdown as
rendering it (PRC-004): CPU/address-space rlimits, sockets disabled,
input opened read-only. The parent enforces the wall-clock budget by
killing this process.

Coordinates are emitted in the VIEWER's space: this module computes the
exact scale the render sandbox uses for the same limits (dpi/72, shrunk
by 0.8 until the page fits the pixel budget) and the exact raster
dimensions pypdfium2 produces (``ceil(size_pt * scale)``), then flips
pdfium's bottom-left page points into top-left raster pixels. A span's
polygon therefore lands on the same pixels the PRC-005 page raster
shows.

Per-page quality facts: ``chars_total`` (character cells pdfium found),
``chars_printable`` (cells that mapped to real text), ``coverage``
(printable / meaningful cells; 0.0 for a page with no native text at
all — the honest routing signal that native text gives you nothing
there).

Output: one JSON manifest on stdout. Exit codes: 0 success, 2 malformed
(shared with the render sandbox), 4 password-protected; anything else
is a crash the parent classifies as operational.
"""

import json
import math
import sys

from soa_worker.render_sandbox import EXIT_MALFORMED, _lock_down

EXIT_ENCRYPTED = 4

#: Characters that pdfium inserts or that carry no text value either
#: way; they count neither as printable nor as unmappable.
_NEUTRAL = {"\r", "\n", "\t", " "}


def _fail(code: int, message: str) -> None:
    print(json.dumps({"error": message}))
    sys.exit(code)


def _viewer_scale(width_pt: float, height_pt: float, max_pixels: int, dpi: int) -> float:
    """The exact scale the render sandbox uses for this page."""
    scale = dpi / 72.0
    while (width_pt * scale) * (height_pt * scale) > max_pixels and scale > 0.1:
        scale *= 0.8
    return scale


def _extract(input_path: str, max_pages: int, max_pixels: int, dpi: int) -> dict[str, object]:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    try:
        document = pdfium.PdfDocument(input_path)
    except pdfium.PdfiumError:
        if pdfium_raw.FPDF_GetLastError() == pdfium_raw.FPDF_ERR_PASSWORD:
            _fail(EXIT_ENCRYPTED, "the document is password-protected")
        _fail(EXIT_MALFORMED, "the file could not be parsed as a PDF")
        raise  # unreachable; keeps type-checkers honest

    total_pages = len(document)
    pages: list[dict[str, object]] = []
    for index in range(min(total_pages, max_pages)):
        page = document[index]
        width_pt, height_pt = page.get_size()
        scale = _viewer_scale(width_pt, height_pt, max_pixels, dpi)
        width_px = math.ceil(width_pt * scale)
        height_px = math.ceil(height_pt * scale)

        textpage = page.get_textpage()
        chars_total = textpage.count_chars()
        spans: list[dict[str, object]] = []
        printable = 0
        meaningful = 0
        if chars_total:
            full_text = textpage.get_text_range()
            for char in full_text:
                if char in _NEUTRAL:
                    continue
                meaningful += 1
                if ord(char) >= 32 and char != "�":
                    printable += 1
            # Cells pdfium counted but could not even emit are
            # unmappable by definition.
            meaningful += max(chars_total - len(full_text), 0)

            for rect_index in range(textpage.count_rects()):
                left, bottom, right, top = textpage.get_rect(rect_index)
                text = textpage.get_text_bounded(
                    left=left, bottom=bottom, right=right, top=top
                ).strip()
                if not text:
                    continue
                # Points (bottom-left origin) -> raster pixels
                # (top-left origin), clamped to the page raster.
                x0 = min(max(left * scale, 0.0), width_px)
                x1 = min(max(right * scale, 0.0), width_px)
                y0 = min(max((height_pt - top) * scale, 0.0), height_px)
                y1 = min(max((height_pt - bottom) * scale, 0.0), height_px)
                spans.append(
                    {
                        "text": text,
                        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                    }
                )

        coverage = (printable / meaningful) if meaningful else 0.0
        pages.append(
            {
                "page_number": index + 1,
                "width_px": width_px,
                "height_px": height_px,
                "coverage": round(coverage, 4),
                "chars_total": chars_total,
                "chars_printable": printable,
                "spans": spans,
            }
        )

    return {"pages": pages, "total_pages": total_pages, "truncated": total_pages > max_pages}


def main() -> None:
    input_path, max_pages, max_pixels, dpi, cpu_seconds, memory_bytes = sys.argv[1:7]
    _lock_down(int(cpu_seconds), int(memory_bytes))
    manifest = _extract(input_path, int(max_pages), int(max_pixels), int(dpi))
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
