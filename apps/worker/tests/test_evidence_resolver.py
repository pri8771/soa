"""Evidence resolver tests (AIO-014): exact match, duplicate quote
ambiguity, OCR variance, no-match page fallback, and the no-fabrication
guarantee — resolved polygons are always built from real text units."""

from soa_worker.evidence_resolver import (
    AMBIGUOUS_CONFIDENCE,
    EXACT_CONFIDENCE,
    PositionedText,
    TextGeometry,
    geometry_from_native_page,
    geometry_from_ocr_page,
    resolve_quote,
)
from soa_worker.providers.native_text import NativeTextPage, TextSpan
from soa_worker.providers.ocr import (
    OcrBlock,
    OcrLine,
    OcrPageInput,
    OcrPageResult,
    OcrWord,
)


def rect(x: float, y: float, w: float, h: float) -> tuple[tuple[float, float], ...]:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def word_geometry() -> TextGeometry:
    """OCR-like word units at known positions on a 1000x1400 page."""
    return TextGeometry(
        page_number=1,
        width_px=1000,
        height_px=1400,
        units=(
            PositionedText("PURCHASE", rect(40, 50, 120, 20)),
            PositionedText("ORDER", rect(170, 50, 80, 20)),
            PositionedText("PO-4711", rect(260, 50, 90, 20)),
            PositionedText("Buyer:", rect(40, 100, 70, 20)),
            PositionedText("ACME", rect(120, 100, 60, 20)),
            PositionedText("Total:", rect(40, 150, 70, 20)),
            PositionedText("62.50", rect(120, 150, 60, 20)),
            PositionedText("ACME", rect(40, 1300, 60, 20)),  # footer: duplicate
        ),
    )


class TestExactMatching:
    def test_a_unique_single_word_resolves_to_its_box(self) -> None:
        resolved = resolve_quote("PO-4711", word_geometry())
        assert resolved.match_kind == "exact"
        assert resolved.polygon == rect(260, 50, 90, 20)
        assert resolved.coordinate_confidence == EXACT_CONFIDENCE
        assert resolved.ambiguous is False
        assert resolved.candidates == (resolved.polygon,)

    def test_a_multi_word_quote_resolves_to_the_union_box(self) -> None:
        resolved = resolve_quote("Total: 62.50", word_geometry())
        assert resolved.match_kind == "exact"
        # Bounding box spans both words.
        assert resolved.polygon == rect(40, 150, 140, 20)

    def test_matching_ignores_case_and_edge_punctuation(self) -> None:
        resolved = resolve_quote("purchase order", word_geometry())
        assert resolved.match_kind == "exact"
        assert resolved.polygon == rect(40, 50, 210, 20)


class TestAmbiguity:
    def test_a_duplicate_quote_is_flagged_with_every_candidate(self) -> None:
        resolved = resolve_quote("ACME", word_geometry())
        assert resolved.match_kind == "exact"
        assert resolved.ambiguous is True
        assert resolved.coordinate_confidence == AMBIGUOUS_CONFIDENCE
        # First occurrence in reading order is primary; both attached.
        assert resolved.polygon == rect(120, 100, 60, 20)
        assert rect(40, 1300, 60, 20) in resolved.candidates
        assert len(resolved.candidates) == 2
        assert "2 times" in resolved.note


class TestOcrVariance:
    def test_a_slightly_misread_token_still_anchors_fuzzily(self) -> None:
        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("PO-47l1", rect(260, 50, 90, 20)),  # OCR read l for 1
                PositionedText("Buyer:", rect(40, 100, 70, 20)),
            ),
        )
        resolved = resolve_quote("PO-4711", geometry)
        assert resolved.match_kind == "fuzzy"
        assert resolved.polygon == rect(260, 50, 90, 20)
        assert 0.7 <= resolved.coordinate_confidence < EXACT_CONFIDENCE
        assert "similarity" in resolved.note


class TestPageFallback:
    def test_no_match_falls_back_to_the_page_explicitly(self) -> None:
        resolved = resolve_quote("unicorn stables", word_geometry())
        assert resolved.match_kind == "page"
        assert resolved.polygon == rect(0, 0, 1000, 1400)
        assert resolved.coordinate_confidence == 0.0
        assert "page-level fallback" in resolved.note

    def test_a_page_without_text_geometry_falls_back(self) -> None:
        empty = TextGeometry(page_number=2, width_px=800, height_px=600, units=())
        resolved = resolve_quote("anything", empty)
        assert resolved.match_kind == "page"
        assert "no positioned text" in resolved.note

    def test_an_empty_quote_falls_back(self) -> None:
        resolved = resolve_quote("   ", word_geometry())
        assert resolved.match_kind == "page"


class TestNoFabrication:
    def test_resolved_polygons_are_built_only_from_real_unit_boxes(self) -> None:
        geometry = word_geometry()
        unit_xs = {x for unit in geometry.units for x, _ in unit.polygon}
        unit_ys = {y for unit in geometry.units for _, y in unit.polygon}
        for quote in ("PO-4711", "Total: 62.50", "PURCHASE ORDER PO-4711"):
            resolved = resolve_quote(quote, geometry)
            assert resolved.match_kind == "exact"
            for x, y in resolved.polygon:
                assert x in unit_xs and y in unit_ys, (
                    "every resolved vertex must come from a real text unit"
                )


class TestGeometryBuilders:
    def test_from_native_text_page(self) -> None:
        page = NativeTextPage(
            page_number=1,
            width_px=1000,
            height_px=1400,
            spans=(TextSpan("PURCHASE ORDER PO-4711", rect(40, 50, 310, 20)),),
            coverage=1.0,
        )
        geometry = geometry_from_native_page(page)
        resolved = resolve_quote("PO-4711", geometry)
        # Span-level granularity: the anchor is the span's own box.
        assert resolved.match_kind == "exact"
        assert resolved.polygon == rect(40, 50, 310, 20)

    def test_from_ocr_page(self) -> None:
        word = OcrWord(text="PO-4711", polygon=rect(260, 50, 90, 20), confidence=0.9)
        line = OcrLine(text="PO-4711", words=(word,), polygon=rect(260, 50, 90, 20), confidence=0.9)
        page_input = OcrPageInput(
            page_number=1, width_px=1000, height_px=1400, image=b"i", content_type="image/png"
        )
        result = OcrPageResult(
            page_number=1,
            blocks=(OcrBlock(lines=(line,), polygon=rect(0, 0, 1000, 1400)),),
            languages=("en",),
        )
        geometry = geometry_from_ocr_page(page_input, result)
        resolved = resolve_quote("PO-4711", geometry)
        assert resolved.match_kind == "exact"
        assert resolved.polygon == rect(260, 50, 90, 20)
