"""Sandboxed rendering child process (PRC-004).

Runs as ``python -m soa_worker.render_sandbox <input> <content_type>
<output_dir> <max_pages> <max_pixels_per_page> <dpi> <cpu_seconds>
<memory_bytes>`` and NEVER in the worker process: the parent enforces a
wall-clock timeout by killing this process's whole group; this process
constrains itself before touching any renderer via the shared
``soa_worker.sandbox`` lockdown (SEC-004): CPU/address-space/file-size/
open-file rlimits, no core dumps, no process creation, sockets and
fork/exec disabled at the python level, a secret-free environment, and
the input file opened read-only and never written.

Renderers: PDFium via pypdfium2 (does not execute embedded JavaScript
or other active content — pages are rasterized, scripts are inert
objects) and Pillow for single/multi-frame images with
``MAX_IMAGE_PIXELS`` set to the per-page pixel budget so decompression
bombs raise instead of allocating.

Output: page PNGs in the output directory plus one JSON manifest on
stdout. Exit codes: 0 success, 2 malformed input, 3 limit exceeded,
anything else is a crash the parent classifies.
"""

import json
import sys

from soa_worker.sandbox import lock_down_python_child as _lock_down

EXIT_MALFORMED = 2
EXIT_LIMIT = 3


def _fail(code: int, message: str) -> None:
    print(json.dumps({"error": message}))
    sys.exit(code)


def _render_pdf(
    input_path: str, output_dir: str, max_pages: int, max_pixels: int, dpi: int
) -> list[dict[str, object]]:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(input_path)
    except Exception:
        _fail(EXIT_MALFORMED, "the file could not be parsed as a PDF")
        raise  # unreachable; keeps type-checkers honest
    if len(document) > max_pages:
        _fail(EXIT_LIMIT, f"document has {len(document)} pages; the limit is {max_pages}")
    pages: list[dict[str, object]] = []
    for index, page in enumerate(document):
        width_pt, height_pt = page.get_size()
        scale = dpi / 72.0
        # Bound the raster: shrink the scale until the page fits the
        # pixel budget instead of allocating an unbounded bitmap.
        while (width_pt * scale) * (height_pt * scale) > max_pixels and scale > 0.1:
            scale *= 0.8
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        path = f"{output_dir}/page-{index + 1:04}.png"
        image.save(path, format="PNG")
        pages.append(
            {
                "page_number": index + 1,
                "width_px": image.width,
                "height_px": image.height,
                "file": path,
                "dpi": round(72 * scale),
            }
        )
    return pages


def _render_image(
    input_path: str, output_dir: str, max_pages: int, max_pixels: int
) -> list[dict[str, object]]:
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = max_pixels  # decompression bombs raise
    try:
        source = Image.open(input_path)
        frames = getattr(source, "n_frames", 1)
        if frames > max_pages:
            _fail(EXIT_LIMIT, f"image has {frames} frames; the limit is {max_pages}")
        pages: list[dict[str, object]] = []
        for index in range(frames):
            source.seek(index)
            frame = source.convert("RGB")
            path = f"{output_dir}/page-{index + 1:04}.png"
            frame.save(path, format="PNG")
            pages.append(
                {
                    "page_number": index + 1,
                    "width_px": frame.width,
                    "height_px": frame.height,
                    "file": path,
                    "dpi": None,
                }
            )
        return pages
    except Image.DecompressionBombError:
        _fail(EXIT_LIMIT, "image raster exceeds the pixel budget")
        raise
    except (UnidentifiedImageError, OSError):
        _fail(EXIT_MALFORMED, "the file could not be decoded as an image")
        raise


def main() -> None:
    (
        input_path,
        content_type,
        output_dir,
        max_pages,
        max_pixels,
        dpi,
        cpu_seconds,
        memory_bytes,
    ) = sys.argv[1:9]
    _lock_down(int(cpu_seconds), int(memory_bytes))
    if content_type == "application/pdf":
        pages = _render_pdf(input_path, output_dir, int(max_pages), int(max_pixels), int(dpi))
    else:
        pages = _render_image(input_path, output_dir, int(max_pages), int(max_pixels))
    print(json.dumps({"pages": pages}))


if __name__ == "__main__":
    main()
