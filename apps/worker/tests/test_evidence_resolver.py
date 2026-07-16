"""Evidence resolver tests (AIO-014): exact match, duplicate quote
ambiguity, OCR variance, no-match page fallback, and the no-fabrication
guarantee — resolved polygons are always built from real text units."""

from soa_db.extracted_fields import EvidenceCertainty
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


class TestResolveEvidenceValueAnchoring:
    """`_resolve_evidence` (pipeline) anchors to the field VALUE first and
    only falls back to the model's verbatim quote. A rambling quote whose
    tokens are scattered across the page must NOT drag the highlight into a
    half-page bounding box when the value itself resolves tightly."""

    @staticmethod
    def _currency_geometry() -> TextGeometry:
        # "GBP" sits tightly in the cost header; the model's quote also drags
        # in "86.000", "ORDER VALUE 60200.00" far below — a big scatter.
        return TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("Cost", rect(500, 900, 60, 20)),
                PositionedText("GBP", rect(570, 900, 50, 20)),  # the value, tight
                PositionedText("86.000", rect(500, 940, 70, 20)),
                PositionedText("ORDER", rect(200, 1250, 90, 20)),
                PositionedText("VALUE", rect(300, 1250, 90, 20)),
                PositionedText("60200.00", rect(200, 1290, 100, 20)),
                PositionedText("stated", rect(200, 1330, 80, 20)),
            ),
        )

    def test_anchors_to_the_value_not_the_rambling_quote(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        span = EvidenceSpan(
            page_number=1,
            polygon=rect(0, 0, 1000, 1400),  # model gave only a page-level box
            quote="Cost GBP 86.000 ORDER VALUE 60200.00 stated",
        )
        evidence = _resolve_evidence(span, {1: self._currency_geometry()}, value="GBP")
        assert evidence.certainty is EvidenceCertainty.REGION
        # Tight box on the value token, not the scattered-quote bounding box.
        assert evidence.polygon == rect(570, 900, 50, 20)
        # The verbatim quote is preserved for provenance/debugging.
        assert evidence.quote == "Cost GBP 86.000 ORDER VALUE 60200.00 stated"

    def test_lightly_normalized_value_still_anchors_via_fuzzy(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        # A value the page spells with a thousands separator ("1,234.50")
        # still anchors to that token — the resolver tolerates the comma as
        # OCR/format variance, so the highlight lands on the amount.
        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("Total:", rect(40, 150, 70, 20)),
                PositionedText("1,234.50", rect(120, 150, 80, 20)),
            ),
        )
        span = EvidenceSpan(page_number=1, polygon=rect(0, 0, 1000, 1400), quote="Total: 1,234.50")
        evidence = _resolve_evidence(span, {1: geometry}, value="1234.50")
        assert evidence.certainty is EvidenceCertainty.REGION
        # Tight on the amount token, not the "Total: 1,234.50" quote union.
        assert evidence.polygon == rect(120, 150, 80, 20)

    def test_falls_back_to_the_quote_when_the_value_is_absent(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        # The value ("APPROVED", a derived status) never appears on the page,
        # but the model's quote does — so we still resolve a region, via the
        # quote, rather than dropping to a page-level box.
        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("DATE", rect(40, 150, 50, 20)),
                PositionedText("02/03/26", rect(100, 150, 90, 20)),
            ),
        )
        span = EvidenceSpan(page_number=1, polygon=rect(0, 0, 1000, 1400), quote="DATE 02/03/26")
        evidence = _resolve_evidence(span, {1: geometry}, value="APPROVED")
        assert evidence.certainty is EvidenceCertainty.REGION
        assert evidence.polygon == rect(40, 150, 150, 20)

    def test_page_fallback_when_neither_value_nor_quote_match(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(PositionedText("Nothing", rect(40, 150, 70, 20)),),
        )
        span = EvidenceSpan(page_number=1, polygon=rect(0, 0, 1000, 1400), quote="absent quote")
        evidence = _resolve_evidence(span, {1: geometry}, value="also-absent")
        assert evidence.certainty is EvidenceCertainty.PAGE
        assert evidence.polygon is None

    def test_ambiguous_value_is_disambiguated_by_the_quote_region(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        # A bare "3" appears in the shipping address AND as the quantity. The
        # value alone is ambiguous and would otherwise anchor to the FIRST "3"
        # (the address). The quote "Quantity: 3" resolves to the quantity line,
        # so the region disambiguates to the correct "3".
        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("Ship", rect(40, 100, 60, 20)),
                PositionedText("To:", rect(110, 100, 40, 20)),
                PositionedText("3", rect(160, 100, 20, 20)),  # address "3" (first)
                PositionedText("Maple", rect(190, 100, 80, 20)),
                PositionedText("Street", rect(280, 100, 80, 20)),
                PositionedText("Quantity:", rect(40, 300, 110, 20)),
                PositionedText("3", rect(160, 300, 20, 20)),  # the real quantity
            ),
        )
        span = EvidenceSpan(page_number=1, polygon=rect(0, 0, 1000, 1400), quote="Quantity: 3")
        evidence = _resolve_evidence(span, {1: geometry}, value="3")
        assert evidence.certainty is EvidenceCertainty.REGION
        # The quantity "3", NOT the address "3" (which is first in reading order).
        assert evidence.polygon == rect(160, 300, 20, 20)

    def test_ambiguous_value_inside_a_broad_quote_takes_first_occurrence(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        # "GBP" appears twice and the model's broad quote covers BOTH — no way
        # (and no need) to prefer one currency token over the other, so we take
        # the first occurrence inside the region: still a tight, real box.
        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("Cost", rect(500, 900, 60, 20)),
                PositionedText("GBP", rect(570, 900, 50, 20)),  # first GBP
                PositionedText("86.000", rect(500, 940, 70, 20)),
                PositionedText("otherwise", rect(120, 1100, 120, 20)),
                PositionedText("GBP", rect(300, 1100, 50, 20)),  # second GBP
                PositionedText("stated", rect(200, 1150, 80, 20)),
            ),
        )
        span = EvidenceSpan(
            page_number=1,
            polygon=rect(0, 0, 1000, 1400),
            quote="Cost GBP 86.000 otherwise GBP stated",
        )
        evidence = _resolve_evidence(span, {1: geometry}, value="GBP")
        assert evidence.certainty is EvidenceCertainty.REGION
        assert evidence.polygon == rect(570, 900, 50, 20)

    def test_multi_span_field_keeps_each_span_at_its_own_location(self) -> None:
        from soa_worker.extraction.provider import EvidenceSpan
        from soa_worker.pipeline import _resolve_evidence

        # The same amount is printed twice (summary box + totals table). Each
        # span's own quote disambiguates it to its own occurrence, so the two
        # stored regions do NOT collapse onto the first "60200.00".
        geometry = TextGeometry(
            page_number=1,
            width_px=1000,
            height_px=1400,
            units=(
                PositionedText("Order", rect(40, 200, 70, 20)),
                PositionedText("Total", rect(120, 200, 80, 20)),
                PositionedText("60200.00", rect(220, 200, 100, 20)),  # summary
                PositionedText("TOTAL", rect(40, 600, 80, 20)),
                PositionedText("DUE", rect(130, 600, 60, 20)),
                PositionedText("60200.00", rect(200, 600, 100, 20)),  # totals table
            ),
        )
        span_a = EvidenceSpan(
            page_number=1, polygon=rect(0, 0, 1000, 1400), quote="Order Total 60200.00"
        )
        span_b = EvidenceSpan(
            page_number=1, polygon=rect(0, 0, 1000, 1400), quote="TOTAL DUE 60200.00"
        )
        ev_a = _resolve_evidence(span_a, {1: geometry}, value="60200.00")
        ev_b = _resolve_evidence(span_b, {1: geometry}, value="60200.00")
        assert ev_a.polygon == rect(220, 200, 100, 20)
        assert ev_b.polygon == rect(200, 600, 100, 20)
        assert ev_a.polygon != ev_b.polygon
