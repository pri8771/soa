"""Native PDF text adapter (AIO-002) — the parent side of the sandbox.

Implements the AIO-001 :class:`NativeTextProvider` contract on PDFium,
running the actual parsing in the constrained child process
(``soa_worker.native_text_sandbox``: rlimits, no sockets, read-only
input) exactly like page rendering does. The adapter shares
:class:`~soa_worker.rendering.RenderLimits` with the renderer so the
text coordinate space and the viewer's page raster are computed from
the SAME dpi / pixel-budget parameters — a span polygon lands on the
pixels the rendered page shows.

Classification is honest and closed:

- password-protected → ``encrypted`` (terminal);
- unparseable → ``corrupt`` (terminal);
- the document has character cells but almost none map to text
  (Type0 fonts without a unicode map, scanned wrappers) →
  ``fonts_unmappable`` (terminal) — a garbage "success" would poison
  extraction downstream;
- non-PDF content types → ``unsupported`` (native text is a PDF
  capability; images go to OCR);
- timeout / crash → ``unavailable`` (retryable, says nothing about the
  document).
"""

import asyncio
import json
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from soa_worker.native_text_sandbox import EXIT_ENCRYPTED
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextFailure,
    NativeTextPage,
    NativeTextRequest,
    NativeTextResult,
    TextSpan,
)
from soa_worker.render_sandbox import EXIT_MALFORMED
from soa_worker.rendering import RenderLimits
from soa_worker.runtime_provenance import package_version
from soa_worker.sandbox import kill_process_tree, python_child_environment

PROVIDER_NAME = "pdfium-native-text"

#: Below this fraction of mappable characters (given enough cells to
#: judge) the document's fonts are effectively unextractable.
_MIN_MAPPABLE_FRACTION = 0.2
_MIN_CELLS_TO_JUDGE = 8

#: Test seam, mirroring soa_worker.rendering._SANDBOX_ARGV: overridden
#: to exercise the timeout/kill path with a child that deliberately
#: hangs. Production always uses the real sandbox.
_SANDBOX_ARGV: list[str] | None = None


class PdfiumNativeTextProvider:
    """See module docstring. ``limits`` should be the same RenderLimits
    the rendering stage uses so coordinates match the viewer raster."""

    def __init__(self, limits: RenderLimits | None = None) -> None:
        self._limits = limits or RenderLimits()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def read(self, request: NativeTextRequest) -> NativeTextResult:
        if request.content_type != "application/pdf":
            raise NativeTextError(
                f"native text extraction does not support {request.content_type}; "
                "non-PDF documents are recognised via OCR",
                failure=NativeTextFailure.UNSUPPORTED,
            )
        manifest = await self._run_sandboxed(request)
        return self._to_result(request, manifest)

    async def _run_sandboxed(self, request: NativeTextRequest) -> dict[str, Any]:
        limits = self._limits
        with tempfile.TemporaryDirectory(prefix="soa-native-text-") as workdir:
            input_path = Path(workdir) / "input.pdf"
            input_path.write_bytes(request.data)
            # The original is read-only for the child; it can never be modified.
            input_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            child_tmp = Path(workdir) / "tmp"
            child_tmp.mkdir()

            argv = _SANDBOX_ARGV or [sys.executable, "-m", "soa_worker.native_text_sandbox"]
            # SEC-004 launch profile: secret-free environment, temp/home
            # confined to the per-run scratch dir, own session so a
            # timeout kills the whole process group.
            process = await asyncio.create_subprocess_exec(
                *argv,
                str(input_path),
                str(request.max_pages),
                str(limits.max_pixels_per_page),
                str(limits.dpi),
                str(limits.cpu_seconds),
                str(limits.memory_bytes),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=python_child_environment(str(child_tmp)),
                cwd=workdir,
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(), timeout=limits.timeout_seconds
                )
            except TimeoutError:
                kill_process_tree(process)
                await process.wait()
                raise NativeTextError(
                    f"native text extraction exceeded the {limits.timeout_seconds:.0f}s budget",
                    failure=NativeTextFailure.UNAVAILABLE,
                    retryable=True,
                ) from None

        payload: dict[str, Any] = {}
        try:
            payload = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            payload = {}
        if process.returncode == EXIT_ENCRYPTED:
            raise NativeTextError(
                "the document is password-protected and its text cannot be read",
                failure=NativeTextFailure.ENCRYPTED,
            )
        if process.returncode == EXIT_MALFORMED:
            raise NativeTextError(
                "the file could not be parsed as a PDF",
                failure=NativeTextFailure.CORRUPT,
            )
        if process.returncode != 0 or "pages" not in payload:
            raise NativeTextError(
                f"the text extractor exited with status {process.returncode}",
                failure=NativeTextFailure.UNAVAILABLE,
                retryable=True,
            )
        return payload

    def _to_result(self, request: NativeTextRequest, manifest: dict[str, Any]) -> NativeTextResult:
        entries = manifest["pages"]
        cells_total = sum(int(entry["chars_total"]) for entry in entries)
        cells_printable = sum(int(entry["chars_printable"]) for entry in entries)
        if cells_total >= _MIN_CELLS_TO_JUDGE and (
            cells_printable / cells_total < _MIN_MAPPABLE_FRACTION
        ):
            raise NativeTextError(
                "the document's fonts cannot be mapped to text "
                f"({cells_printable} of {cells_total} characters extractable); "
                "recognise it via OCR instead",
                failure=NativeTextFailure.FONTS_UNMAPPABLE,
            )

        pages = []
        for entry in entries:
            spans = tuple(
                TextSpan(
                    text=str(span["text"]),
                    polygon=tuple((float(x), float(y)) for x, y in span["polygon"]),
                )
                for span in entry["spans"]
            )
            pages.append(
                NativeTextPage(
                    page_number=int(entry["page_number"]),
                    width_px=int(entry["width_px"]),
                    height_px=int(entry["height_px"]),
                    spans=spans,
                    coverage=float(entry["coverage"]),
                )
            )

        warnings = []
        if manifest.get("truncated"):
            warnings.append(
                f"document has {manifest['total_pages']} pages; text was read from the "
                f"first {request.max_pages} only"
            )
        empty = sum(1 for page in pages if not page.spans)
        if pages and empty:
            warnings.append(
                f"{empty} of {len(pages)} pages carry no native text (scanned or image-only)"
            )
        return NativeTextResult(
            provider=self.name,
            pages=tuple(pages),
            model=f"pdfium {package_version('pypdfium2')}",
            warnings=tuple(warnings),
        )


__all__ = ["PROVIDER_NAME", "PdfiumNativeTextProvider"]
