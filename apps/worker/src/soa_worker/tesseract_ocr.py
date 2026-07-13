"""Tesseract OCR adapter (AIO-004).

Implements the AIO-001 :class:`OcrProvider` contract on the ``tesseract``
CLI (a local process — content never leaves the deployment). Pages are
recognised one at a time from their PNG rasters; the TSV output's
block/paragraph/line/word hierarchy maps onto the contract's
block → line → word DTOs with pixel coordinates (already top-left
origin, the PRC-005 system) and per-word confidence.

Boundaries and honesty:

- languages are fail-closed at two levels: a tag the adapter has no
  tesseract pack name for is refused, and a mapped pack that is not
  installed is refused naming what IS installed — a stream can only
  configure languages the deployment can actually recognise;
- each page run is resource-limited (CPU seconds and address space via
  rlimits, single-threaded via ``OMP_THREAD_LIMIT``) and wall-clock
  bounded — a hung engine is killed and reported retryable;
- confidence is tesseract's own word confidence scaled to [0, 1],
  never rescaled to look better.

The adapter registers only when the binary is present (see
``soa_worker.providers.builtin``): a missing engine disables the OCR
capability clearly instead of failing at first use.
"""

import asyncio
import os
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from soa_worker.providers.ocr import (
    OcrBlock,
    OcrLine,
    OcrPageInput,
    OcrPageResult,
    OcrProviderError,
    OcrRequest,
    OcrResult,
    OcrWord,
)

PROVIDER_NAME = "tesseract"

#: Language tags (the platform's lowercase tags) → tesseract pack names.
#: Only tags listed here can ever be configured for this adapter.
LANGUAGE_PACKS = {
    "en": "eng",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "nl": "nld",
    "pt": "por",
    "pl": "pol",
}


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


@lru_cache(maxsize=1)
def installed_language_tags() -> tuple[str, ...]:
    """The platform tags whose tesseract packs are actually installed.
    Empty when the binary is missing — the capability then never
    registers."""
    if not tesseract_available():
        return ()
    try:
        listing = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    installed = {line.strip() for line in listing.stdout.splitlines() if line.strip()}
    return tuple(sorted(tag for tag, pack in LANGUAGE_PACKS.items() if pack in installed))


@dataclass(frozen=True)
class OcrLimits:
    timeout_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 1_073_741_824  # 1 GiB address space


@dataclass(frozen=True)
class _TsvRow:
    level: int
    block: int
    par: int
    line: int
    word: int
    left: int
    top: int
    width: int
    height: int
    conf: float
    text: str


def _parse_tsv(tsv: str) -> list[_TsvRow]:
    rows: list[_TsvRow] = []
    lines = tsv.splitlines()
    for line in lines[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        rows.append(
            _TsvRow(
                level=int(parts[0]),
                block=int(parts[2]),
                par=int(parts[3]),
                line=int(parts[4]),
                word=int(parts[5]),
                left=int(parts[6]),
                top=int(parts[7]),
                width=int(parts[8]),
                height=int(parts[9]),
                conf=float(parts[10]),
                text=parts[11],
            )
        )
    return rows


def _polygon(
    row: _TsvRow, page: OcrPageInput
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]:
    x0 = float(min(max(row.left, 0), page.width_px))
    y0 = float(min(max(row.top, 0), page.height_px))
    x1 = float(min(max(row.left + row.width, 0), page.width_px))
    y1 = float(min(max(row.top + row.height, 0), page.height_px))
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _page_result(
    page: OcrPageInput, rows: list[_TsvRow], languages: tuple[str, ...]
) -> OcrPageResult:
    block_boxes = {row.block: row for row in rows if row.level == 2}
    line_boxes = {(row.block, row.par, row.line): row for row in rows if row.level == 4}
    words_by_line: dict[tuple[int, int, int], list[OcrWord]] = {}
    line_order: list[tuple[int, int, int]] = []
    for row in rows:
        if row.level != 5 or not row.text.strip():
            continue
        key = (row.block, row.par, row.line)
        if key not in words_by_line:
            words_by_line[key] = []
            line_order.append(key)
        words_by_line[key].append(
            OcrWord(
                text=row.text.strip(),
                polygon=_polygon(row, page),
                confidence=min(max(row.conf, 0.0), 100.0) / 100.0,
            )
        )

    lines_by_block: dict[int, list[OcrLine]] = {}
    block_order: list[int] = []
    for key in line_order:
        words = words_by_line[key]
        box_row = line_boxes.get(key)
        polygon = _polygon(box_row, page) if box_row else words[0].polygon
        line = OcrLine(
            text=" ".join(word.text for word in words),
            words=tuple(words),
            polygon=polygon,
            confidence=sum(word.confidence for word in words) / len(words),
        )
        block_number = key[0]
        if block_number not in lines_by_block:
            lines_by_block[block_number] = []
            block_order.append(block_number)
        lines_by_block[block_number].append(line)

    blocks = []
    for block_number in block_order:
        lines = lines_by_block[block_number]
        box_row = block_boxes.get(block_number)
        polygon = _polygon(box_row, page) if box_row else lines[0].polygon
        blocks.append(OcrBlock(lines=tuple(lines), polygon=polygon))
    return OcrPageResult(page_number=page.page_number, blocks=tuple(blocks), languages=languages)


class TesseractOcrProvider:
    """See module docstring."""

    def __init__(self, limits: OcrLimits | None = None) -> None:
        self._limits = limits or OcrLimits()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def _packs_for(self, languages: tuple[str, ...]) -> str:
        installed = installed_language_tags()
        for tag in languages:
            if tag not in LANGUAGE_PACKS:
                raise OcrProviderError(
                    f"language {tag!r} is not supported by the tesseract adapter",
                    retryable=False,
                )
            if tag not in installed:
                raise OcrProviderError(
                    f"the tesseract language pack for {tag!r} is not installed "
                    f"(installed: {', '.join(installed) or '(none)'})",
                    retryable=False,
                )
        return "+".join(LANGUAGE_PACKS[tag] for tag in languages)

    async def recognize(self, request: OcrRequest) -> OcrResult:
        if not tesseract_available():
            raise OcrProviderError("the tesseract binary is not installed", retryable=False)
        packs = self._packs_for(request.languages)
        pages = []
        for page in sorted(request.pages, key=lambda p: p.page_number):
            tsv = await self._run_page(page, packs)
            pages.append(_page_result(page, _parse_tsv(tsv), request.languages))
        return OcrResult(provider=self.name, pages=tuple(pages), model=_engine_version())

    async def _run_page(self, page: OcrPageInput, packs: str) -> str:
        limits = self._limits

        def _constrain() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))

        with tempfile.TemporaryDirectory(prefix="soa-ocr-") as workdir:
            image_path = Path(workdir) / "page.png"
            image_path.write_bytes(page.image)
            process = await asyncio.create_subprocess_exec(
                "tesseract",
                str(image_path),
                "stdout",
                "-l",
                packs,
                "--psm",
                "3",
                "tsv",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                preexec_fn=_constrain,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(), timeout=limits.timeout_seconds
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise OcrProviderError(
                    f"OCR of page {page.page_number} exceeded the "
                    f"{limits.timeout_seconds:.0f}s budget",
                    retryable=True,
                ) from None
        if process.returncode != 0:
            raise OcrProviderError(
                f"the OCR engine exited with status {process.returncode} "
                f"on page {page.page_number}",
                retryable=True,
            )
        return stdout.decode("utf-8", errors="replace")


@lru_cache(maxsize=1)
def _engine_version() -> str:
    try:
        probe = subprocess.run(
            ["tesseract", "--version"], capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return "tesseract (version unknown)"
    first = (probe.stdout or probe.stderr).splitlines()[0].strip()
    return first or "tesseract (version unknown)"


__all__ = [
    "LANGUAGE_PACKS",
    "PROVIDER_NAME",
    "OcrLimits",
    "TesseractOcrProvider",
    "installed_language_tags",
    "tesseract_available",
]
