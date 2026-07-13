"""Page-rendering adapter (PRC-004) — the parent side of the sandbox.

``render_document`` hands the bytes to a separate constrained process
(soa_worker.render_sandbox: CPU/memory rlimits, sockets disabled, no
active-content execution) and enforces the WALL-CLOCK budget itself by
killing the child on timeout. The original bytes are written to a
read-only temp file the child never modifies; failures come back
classified — malformed input and limit violations are terminal for the
file, timeouts and crashes are retryable operational failures.
"""

import asyncio
import json
import stat
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from soa_worker.render_sandbox import EXIT_LIMIT, EXIT_MALFORMED
from soa_worker.sandbox import kill_process_tree, python_child_environment

RENDERABLE_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/tiff"})


class RenderFailure(StrEnum):
    MALFORMED = "malformed"  # bad file: terminal
    LIMIT_EXCEEDED = "limit_exceeded"  # over page/pixel budget: terminal
    TIMEOUT = "timeout"  # operational: retryable
    CRASHED = "crashed"  # operational: retryable


@dataclass(frozen=True)
class RenderError(Exception):
    failure: RenderFailure
    safe_message: str

    @property
    def retryable(self) -> bool:
        return self.failure in (RenderFailure.TIMEOUT, RenderFailure.CRASHED)

    def __str__(self) -> str:
        return f"{self.failure.value}: {self.safe_message}"


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    width_px: int
    height_px: int
    dpi: int | None
    image_png: bytes


@dataclass(frozen=True)
class RenderLimits:
    max_pages: int = 50
    max_pixels_per_page: int = 25_000_000
    dpi: int = 200
    timeout_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 1_073_741_824  # 1 GiB address space


#: Test seam: overridden to exercise the timeout/kill path with a child
#: that deliberately hangs. Production always uses the real sandbox.
_SANDBOX_ARGV: list[str] | None = None


async def render_document(
    data: bytes, *, content_type: str, limits: RenderLimits | None = None
) -> list[RenderedPage]:
    if content_type not in RENDERABLE_TYPES:
        raise RenderError(RenderFailure.MALFORMED, f"unsupported content type {content_type}")
    effective = limits or RenderLimits()

    with tempfile.TemporaryDirectory(prefix="soa-render-") as workdir:
        input_path = Path(workdir) / "input"
        input_path.write_bytes(data)
        # The original is read-only for the child; it can never be modified.
        input_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        output_dir = Path(workdir) / "pages"
        output_dir.mkdir()
        child_tmp = Path(workdir) / "tmp"
        child_tmp.mkdir()

        argv = _SANDBOX_ARGV or [sys.executable, "-m", "soa_worker.render_sandbox"]
        # SEC-004 launch profile: secret-free environment, temp/home
        # confined to the per-run scratch dir, own session so a timeout
        # kills the whole process group.
        process = await asyncio.create_subprocess_exec(
            *argv,
            str(input_path),
            content_type,
            str(output_dir),
            str(effective.max_pages),
            str(effective.max_pixels_per_page),
            str(effective.dpi),
            str(effective.cpu_seconds),
            str(effective.memory_bytes),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=python_child_environment(str(child_tmp)),
            cwd=workdir,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=effective.timeout_seconds
            )
        except TimeoutError:
            kill_process_tree(process)
            await process.wait()
            raise RenderError(
                RenderFailure.TIMEOUT,
                f"rendering exceeded the {effective.timeout_seconds:.0f}s budget",
            ) from None

        payload: dict[str, object] = {}
        try:
            payload = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            payload = {}
        if process.returncode == EXIT_MALFORMED:
            raise RenderError(RenderFailure.MALFORMED, str(payload.get("error", "malformed input")))
        if process.returncode == EXIT_LIMIT:
            raise RenderError(
                RenderFailure.LIMIT_EXCEEDED, str(payload.get("error", "limit exceeded"))
            )
        if process.returncode != 0 or "pages" not in payload:
            raise RenderError(
                RenderFailure.CRASHED,
                f"renderer exited with status {process.returncode}",
            )

        pages: list[RenderedPage] = []
        manifest = payload["pages"]
        assert isinstance(manifest, list)
        for entry in manifest:
            page_file = Path(str(entry["file"]))
            pages.append(
                RenderedPage(
                    page_number=int(entry["page_number"]),
                    width_px=int(entry["width_px"]),
                    height_px=int(entry["height_px"]),
                    dpi=int(entry["dpi"]) if entry.get("dpi") else None,
                    image_png=page_file.read_bytes(),
                )
            )
        return pages
