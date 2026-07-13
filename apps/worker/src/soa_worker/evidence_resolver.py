"""Evidence resolver (AIO-014): quote → coordinates.

Model-based extraction returns page-level evidence with a verbatim
quote (AIO-007/011): the model knows WHAT it read and on which page,
but has no geometry. This resolver anchors the quote to real
coordinates using the text geometry the deterministic stages produced —
native-text spans (AIO-002) or OCR words (AIO-004).

Honesty rules:

- **no fabricated coordinates** — a resolved polygon is always the
  bounding box of REAL positioned text units that matched the quote;
  when nothing matches, the resolution is an EXPLICIT page-level
  fallback (full page rectangle, ``match_kind="page"``, coordinate
  confidence 0.0) — never a plausible-looking guess;
- **ambiguity is surfaced, not swallowed** — a quote that appears in
  several places resolves to the first occurrence in reading order,
  flagged ``ambiguous`` with EVERY candidate polygon attached and a
  reduced coordinate confidence, so the viewer can show all of them;
- **coordinate confidence is separate from value confidence** — it
  scores only how sure we are about WHERE: exact-and-unique is high,
  fuzzy (OCR variance) carries the similarity ratio, page fallback is
  zero.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

from soa_worker.providers.native_text import NativeTextPage
from soa_worker.providers.ocr import OcrPageInput, OcrPageResult

Polygon = tuple[tuple[float, float], ...]

#: Minimum similarity for a fuzzy (OCR-variance) match.
FUZZY_THRESHOLD = 0.8
EXACT_CONFIDENCE = 0.98
AMBIGUOUS_CONFIDENCE = 0.5


@dataclass(frozen=True)
class PositionedText:
    """One positioned text unit: an OCR word or a native-text span."""

    text: str
    polygon: Polygon


@dataclass(frozen=True)
class TextGeometry:
    """Everything known about where text sits on one page."""

    page_number: int
    width_px: int
    height_px: int
    units: tuple[PositionedText, ...]


@dataclass(frozen=True)
class ResolvedEvidence:
    page_number: int
    #: Bounding box of the primary match, or the full page on fallback.
    polygon: Polygon
    match_kind: str  # exact | fuzzy | page
    #: How sure we are about WHERE (not about the value itself).
    coordinate_confidence: float
    ambiguous: bool
    #: Every plausible location (always includes ``polygon`` first).
    candidates: tuple[Polygon, ...]
    note: str


def geometry_from_native_page(page: NativeTextPage) -> TextGeometry:
    return TextGeometry(
        page_number=page.page_number,
        width_px=page.width_px,
        height_px=page.height_px,
        units=tuple(PositionedText(text=span.text, polygon=span.polygon) for span in page.spans),
    )


def geometry_from_ocr_page(page_input: OcrPageInput, result: OcrPageResult) -> TextGeometry:
    if page_input.page_number != result.page_number:
        raise ValueError("page input and OCR result describe different pages")
    words = tuple(
        PositionedText(text=word.text, polygon=word.polygon)
        for block in result.blocks
        for line in block.lines
        for word in line.words
    )
    return TextGeometry(
        page_number=page_input.page_number,
        width_px=page_input.width_px,
        height_px=page_input.height_px,
        units=words,
    )


@dataclass(frozen=True)
class _Token:
    normalized: str
    polygon: Polygon


def _normalize(token: str) -> str:
    return token.casefold().strip(".,;:()[]\"'")


def _tokens_of(geometry: TextGeometry) -> list[_Token]:
    tokens: list[_Token] = []
    for unit in geometry.units:
        for word in unit.text.split():
            normalized = _normalize(word)
            if normalized:
                tokens.append(_Token(normalized=normalized, polygon=unit.polygon))
    return tokens


def _bounding_box(polygons: list[Polygon]) -> Polygon:
    xs = [x for polygon in polygons for x, _ in polygon]
    ys = [y for polygon in polygons for _, y in polygon]
    return ((min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys)))


def _page_polygon(geometry: TextGeometry) -> Polygon:
    return (
        (0.0, 0.0),
        (float(geometry.width_px), 0.0),
        (float(geometry.width_px), float(geometry.height_px)),
        (0.0, float(geometry.height_px)),
    )


def resolve_quote(quote: str, geometry: TextGeometry) -> ResolvedEvidence:
    """Anchor ``quote`` to coordinates on the page. See module docstring."""
    quote_tokens = [_normalize(token) for token in quote.split()]
    quote_tokens = [token for token in quote_tokens if token]
    tokens = _tokens_of(geometry)

    def page_fallback(reason: str) -> ResolvedEvidence:
        polygon = _page_polygon(geometry)
        return ResolvedEvidence(
            page_number=geometry.page_number,
            polygon=polygon,
            match_kind="page",
            coordinate_confidence=0.0,
            ambiguous=False,
            candidates=(polygon,),
            note=f"page-level fallback: {reason}",
        )

    if not quote_tokens:
        return page_fallback("the quote is empty")
    if not tokens:
        return page_fallback("the page has no positioned text to match against")
    if len(quote_tokens) > len(tokens):
        return page_fallback("the quote is longer than the page's text")

    window = len(quote_tokens)

    # Exact: every contiguous window whose tokens equal the quote's.
    exact_matches: list[Polygon] = []
    for start in range(len(tokens) - window + 1):
        if [token.normalized for token in tokens[start : start + window]] == quote_tokens:
            exact_matches.append(
                _bounding_box([token.polygon for token in tokens[start : start + window]])
            )
    # Physically identical locations (several tokens sharing one span
    # polygon) are one candidate, not an ambiguity.
    unique_exact = list(dict.fromkeys(exact_matches))
    if len(unique_exact) == 1:
        return ResolvedEvidence(
            page_number=geometry.page_number,
            polygon=unique_exact[0],
            match_kind="exact",
            coordinate_confidence=EXACT_CONFIDENCE,
            ambiguous=False,
            candidates=(unique_exact[0],),
            note="exact match, unique on the page",
        )
    if len(unique_exact) > 1:
        return ResolvedEvidence(
            page_number=geometry.page_number,
            polygon=unique_exact[0],
            match_kind="exact",
            coordinate_confidence=AMBIGUOUS_CONFIDENCE,
            ambiguous=True,
            candidates=tuple(unique_exact),
            note=(
                f"the quote appears {len(unique_exact)} times on the page; "
                "resolved to the first occurrence in reading order — all "
                "candidate locations attached"
            ),
        )

    # Fuzzy: best same-size window by similarity (OCR variance).
    quote_joined = " ".join(quote_tokens)
    best_ratio = 0.0
    best_polygon: Polygon | None = None
    for start in range(len(tokens) - window + 1):
        window_tokens = tokens[start : start + window]
        ratio = SequenceMatcher(
            None, " ".join(token.normalized for token in window_tokens), quote_joined
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_polygon = _bounding_box([token.polygon for token in window_tokens])
    if best_polygon is not None and best_ratio >= FUZZY_THRESHOLD:
        return ResolvedEvidence(
            page_number=geometry.page_number,
            polygon=best_polygon,
            match_kind="fuzzy",
            coordinate_confidence=round(best_ratio * 0.9, 4),
            ambiguous=False,
            candidates=(best_polygon,),
            note=f"fuzzy match at {best_ratio:.0%} similarity (OCR variance tolerated)",
        )

    return page_fallback(
        f"no text on the page matches the quote (best similarity {best_ratio:.0%})"
    )


__all__ = [
    "AMBIGUOUS_CONFIDENCE",
    "EXACT_CONFIDENCE",
    "FUZZY_THRESHOLD",
    "PositionedText",
    "ResolvedEvidence",
    "TextGeometry",
    "geometry_from_native_page",
    "geometry_from_ocr_page",
    "resolve_quote",
]
