"""Locate a reviewer's typed value on the page (AIO-014 at review time).

The extracting stage persists per-page positioned text (spans with
polygons) as a ``text_geometry`` artifact. When a reviewer corrects or
fills a field, this resolves the entered value back to a real region on
the page so the viewer can highlight where it came from — the same
honesty rule as extraction evidence: a returned polygon is the bounding
box of REAL positioned text that matched, never a fabricated box. No
match returns ``None`` (the caller falls back to letting the reviewer
draw the box by hand).
"""

from typing import Any

Polygon = list[list[float]]


def _normalize(token: str) -> str:
    return token.casefold().strip(".,;:()[]\"'")


def _tokens(spans: list[dict[str, Any]]) -> list[tuple[str, Polygon]]:
    """Flatten spans into normalized words, each carrying its span's polygon."""
    out: list[tuple[str, Polygon]] = []
    for span in spans:
        polygon = span.get("polygon")
        text = span.get("text")
        if not isinstance(polygon, list) or not isinstance(text, str):
            continue
        for word in text.split():
            normalized = _normalize(word)
            if normalized:
                out.append((normalized, polygon))
    return out


def _bounding_box(polygons: list[Polygon]) -> Polygon:
    xs = [x for polygon in polygons for x, _ in polygon]
    ys = [y for polygon in polygons for _, y in polygon]
    return [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]


def text_in_region(geometry: dict[str, Any], page_number: int, polygon: Polygon) -> str:
    """The positioned text a labeller's drawn box encloses (the inverse of
    :func:`locate_value`: box → text instead of value → box).

    A span is included when its centre falls inside the drawn box's bounding
    rectangle; matched spans are returned in reading order (top-to-bottom,
    then left-to-right) joined by single spaces. Returns "" when the box
    encloses no positioned text — the labeller then types the value by hand,
    never a fabricated one."""
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    if not xs or not ys:
        return ""
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    raw_pages = geometry.get("pages")
    if not isinstance(raw_pages, list):
        return ""
    matched: list[tuple[float, float, str]] = []
    for page in raw_pages:
        if not isinstance(page, dict) or page.get("page_number") != page_number:
            continue
        spans = page.get("spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            span_polygon = span.get("polygon")
            text = span.get("text")
            if not isinstance(span_polygon, list) or not isinstance(text, str) or not span_polygon:
                continue
            sxs = [pt[0] for pt in span_polygon if isinstance(pt, (list, tuple)) and len(pt) == 2]
            sys_ = [pt[1] for pt in span_polygon if isinstance(pt, (list, tuple)) and len(pt) == 2]
            if not sxs or not sys_:
                continue
            centre_x = sum(sxs) / len(sxs)
            centre_y = sum(sys_) / len(sys_)
            if min_x <= centre_x <= max_x and min_y <= centre_y <= max_y:
                matched.append((min(sys_), min(sxs), text.strip()))
    matched.sort()
    return " ".join(text for _, _, text in matched if text).strip()


def locate_value(
    geometry: dict[str, Any], value: str, *, page_hint: int | None = None
) -> dict[str, Any] | None:
    """Find ``value`` in the persisted page geometry.

    Returns ``{"page_number", "polygon"}`` for the first contiguous run of
    words (in reading order) whose normalized text equals the value's, or
    ``None`` when nothing matches. ``page_hint`` searches that page first.
    """
    target = [t for t in (_normalize(w) for w in value.split()) if t]
    if not target:
        return None
    raw_pages = geometry.get("pages")
    if not isinstance(raw_pages, list):
        return None
    pages = [p for p in raw_pages if isinstance(p, dict)]
    if page_hint is not None:
        pages = sorted(pages, key=lambda p: p.get("page_number") != page_hint)

    for page in pages:
        spans = page.get("spans")
        if not isinstance(spans, list):
            continue
        tokens = _tokens(spans)
        window = len(target)
        for start in range(len(tokens) - window + 1):
            if [tok for tok, _ in tokens[start : start + window]] == target:
                box = _bounding_box([poly for _, poly in tokens[start : start + window]])
                return {"page_number": page.get("page_number"), "polygon": box}
    return None
