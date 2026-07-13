"""Image preprocessing for page rasters (AIO-003).

Prepares a rendered page image (PRC-004/005 raster) for OCR without
ever touching the stored original: the caller passes PNG bytes in and
receives NEW bytes out (byte-identical to the input when nothing needed
doing, so callers can skip storing a duplicate). Every change is
recorded in the report — a processed image whose history cannot be
audited is worthless as evidence.

Operations, in order, all deterministic and bounded (detection runs on
a ≤``ANALYSIS_MAX_SIDE``px grayscale copy; the angle search is a fixed
grid):

- **orientation** — if text lines read vertical (row-profile variance
  is decisively higher after a 90° turn), turn the page upright. A 90°
  vs 270° / 180° decision needs reading actual glyphs, which is OCR
  script-detection territory (AIO-004) — the report says so honestly
  instead of guessing.
- **deskew** — small-angle search (±``MAX_SKEW_DEGREES``°) maximising
  the row-profile variance; applied only when the improvement is
  decisive and the angle is at least ``MIN_SKEW_DEGREES``°. Dimensions
  are preserved (no expand), keeping the raster geometry stable.
- **contrast** (opt-in, conservative) — autocontrast only when the
  luminance dynamic range is genuinely poor; a good scan is never
  touched.
- **noise filter** (opt-in, conservative) — a single 3x3 median pass.
- **blank detection** — reported, never acted on: a blank page is a
  routing/review signal, not something to discard here.

The quality report (ink fraction, dynamic range, blank) is measured on
the FINAL image so downstream routing reads the truth about what OCR
will actually receive.
"""

import io
from dataclasses import dataclass, field

from PIL import Image, ImageFilter, ImageOps

ANALYSIS_MAX_SIDE = 800
MAX_SKEW_DEGREES = 5.0
SKEW_STEP_DEGREES = 0.5
MIN_SKEW_DEGREES = 0.5
#: A candidate correction must beat the current profile by this factor.
DECISIVE_IMPROVEMENT = 1.3
#: Luminance below this counts as ink.
INK_LUMINANCE = 200
#: Pages with less ink than this fraction are reported blank.
BLANK_INK_FRACTION = 0.002
#: p98 - p2 luminance spread below this counts as poor contrast.
POOR_DYNAMIC_RANGE = 150


class PreprocessError(Exception):
    """Preprocessing refused the image. The message is display-safe."""


@dataclass(frozen=True)
class PreprocessOptions:
    orient: bool = True
    deskew: bool = True
    #: Conservative enhancements are OPT-IN: a stream must ask for them.
    contrast: bool = False
    noise_filter: bool = False
    #: Same budget as the render pipeline; anything bigger is refused.
    max_pixels: int = 25_000_000


@dataclass(frozen=True)
class QualityReport:
    """Measured on the final image — what OCR will actually receive."""

    ink_fraction: float
    dynamic_range: int
    blank: bool


@dataclass(frozen=True)
class PreprocessReport:
    #: Ordered, human-readable record of every change applied.
    operations: tuple[str, ...]
    rotation_degrees: int
    deskew_degrees: float
    contrast_applied: bool
    noise_filter_applied: bool
    quality: QualityReport
    #: Honest caveats and observations that did NOT change the image.
    notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class PreprocessedImage:
    image_png: bytes
    width_px: int
    height_px: int
    report: PreprocessReport


def _analysis_copy(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    gray.thumbnail((ANALYSIS_MAX_SIDE, ANALYSIS_MAX_SIDE), Image.Resampling.BOX)
    return gray


def _row_profile_variance(gray: Image.Image) -> float:
    """Variance of per-row mean luminance: high when text lines run
    horizontally, low when they are rotated or skewed away."""
    column = gray.resize((1, gray.height), Image.Resampling.BOX)
    values = column.tobytes()  # mode L: one luminance byte per row
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def _percentile(hist: list[int], total: int, fraction: float) -> int:
    target = total * fraction
    cumulative = 0
    for value, count in enumerate(hist):
        cumulative += count
        if cumulative >= target:
            return value
    return 255


def _measure_quality(image: Image.Image) -> QualityReport:
    gray = _analysis_copy(image)
    hist = gray.histogram()
    total = gray.width * gray.height
    ink = sum(hist[:INK_LUMINANCE])
    ink_fraction = ink / total if total else 0.0
    dynamic_range = _percentile(hist, total, 0.98) - _percentile(hist, total, 0.02)
    return QualityReport(
        ink_fraction=round(ink_fraction, 6),
        dynamic_range=dynamic_range,
        blank=ink_fraction < BLANK_INK_FRACTION,
    )


def _detect_skew(gray: Image.Image) -> tuple[float, float, float]:
    """(best angle, base score, best score) over the fixed search grid."""
    base = _row_profile_variance(gray)
    best_angle, best_score = 0.0, base
    steps = int(MAX_SKEW_DEGREES / SKEW_STEP_DEGREES)
    for step in range(-steps, steps + 1):
        angle = step * SKEW_STEP_DEGREES
        if angle == 0.0:
            continue
        candidate = gray.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=255)
        score = _row_profile_variance(candidate)
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle, base, best_score


def preprocess_page_image(
    image_png: bytes, options: PreprocessOptions | None = None
) -> PreprocessedImage:
    """Prepare one page raster for OCR. The input bytes are never
    modified; the returned bytes ARE the input when no change was
    needed."""
    effective = options or PreprocessOptions()
    try:
        decoded = Image.open(io.BytesIO(image_png))
        decoded.load()
    except Exception:
        raise PreprocessError("the page image could not be decoded") from None
    if decoded.width * decoded.height > effective.max_pixels:
        raise PreprocessError(
            f"the page raster ({decoded.width}x{decoded.height}) exceeds the "
            f"{effective.max_pixels} pixel budget"
        )
    image: Image.Image = decoded.convert("RGB")

    operations: list[str] = []
    notes: list[str] = []
    rotation_degrees = 0
    deskew_degrees = 0.0
    contrast_applied = False
    noise_applied = False

    if effective.orient:
        upright = _row_profile_variance(_analysis_copy(image))
        turned = _row_profile_variance(_analysis_copy(image.transpose(Image.Transpose.ROTATE_90)))
        if turned > upright * DECISIVE_IMPROVEMENT:
            image = image.transpose(Image.Transpose.ROTATE_90)
            rotation_degrees = 90
            operations.append("rotated 90° so text lines run horizontally")
            notes.append(
                "90° vs 270° and upside-down (180°) orientation cannot be told apart "
                "without reading glyphs; OCR script detection resolves it downstream"
            )

    if effective.deskew:
        angle, base, best = _detect_skew(_analysis_copy(image))
        if abs(angle) >= MIN_SKEW_DEGREES and best > base * DECISIVE_IMPROVEMENT:
            image = image.rotate(
                angle, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255)
            )
            deskew_degrees = angle
            operations.append(f"deskewed by {angle:+.1f}° (dimensions preserved)")

    if effective.contrast:
        gray = _analysis_copy(image)
        hist = gray.histogram()
        total = gray.width * gray.height
        spread = _percentile(hist, total, 0.98) - _percentile(hist, total, 0.02)
        if spread < POOR_DYNAMIC_RANGE:
            image = ImageOps.autocontrast(image, cutoff=2)
            contrast_applied = True
            operations.append(
                f"stretched contrast (dynamic range was {spread}, "
                f"below the {POOR_DYNAMIC_RANGE} threshold)"
            )
        else:
            notes.append(f"contrast enhancement enabled but not needed (dynamic range {spread})")

    if effective.noise_filter:
        image = image.filter(ImageFilter.MedianFilter(3))
        noise_applied = True
        operations.append("applied a single 3x3 median noise filter")

    quality = _measure_quality(image)
    if quality.blank:
        notes.append("the page appears blank; kept for review, not discarded")

    if operations:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        output = buffer.getvalue()
    else:
        output = image_png  # byte-identical: nothing needed doing

    return PreprocessedImage(
        image_png=output,
        width_px=image.width,
        height_px=image.height,
        report=PreprocessReport(
            operations=tuple(operations),
            rotation_degrees=rotation_degrees,
            deskew_degrees=deskew_degrees,
            contrast_applied=contrast_applied,
            noise_filter_applied=noise_applied,
            quality=quality,
            notes=tuple(notes),
        ),
    )


__all__ = [
    "BLANK_INK_FRACTION",
    "PreprocessError",
    "PreprocessOptions",
    "PreprocessReport",
    "PreprocessedImage",
    "QualityReport",
    "preprocess_page_image",
]
