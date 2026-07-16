"""Locate-value matcher tests (AIO-014 at review time): a typed value
resolves to the bounding box of the REAL positioned text that matches, a
value not on the page returns None (never a fabricated box), and a page
hint is searched first."""

from soa_api.services.locate import locate_value

# Two pages of positioned words. Polygons are [x,y] vertices in raster px.
GEOMETRY = {
    "pages": [
        {
            "page_number": 1,
            "width_px": 1000,
            "height_px": 1400,
            "spans": [
                {"text": "PO", "polygon": [[10, 20], [40, 20], [40, 40], [10, 40]]},
                {"text": "Number:", "polygon": [[45, 20], [120, 20], [120, 40], [45, 40]]},
                {"text": "8077219", "polygon": [[130, 20], [260, 20], [260, 40], [130, 40]]},
                {
                    "text": "Mawdsley-Brooks",
                    "polygon": [[10, 80], [300, 80], [300, 110], [10, 110]],
                },
                {"text": "Ltd", "polygon": [[310, 80], [360, 80], [360, 110], [310, 110]]},
            ],
        },
        {
            "page_number": 2,
            "width_px": 1000,
            "height_px": 1400,
            "spans": [
                {"text": "8077219", "polygon": [[500, 900], [630, 900], [630, 930], [500, 930]]},
            ],
        },
    ]
}


def test_single_token_resolves_to_its_span_box() -> None:
    match = locate_value(GEOMETRY, "8077219")
    assert match == {"page_number": 1, "polygon": [[130, 20], [260, 20], [260, 40], [130, 40]]}


def test_multi_token_value_spans_a_bounding_box() -> None:
    match = locate_value(GEOMETRY, "Mawdsley-Brooks Ltd")
    # Bounding box over both words: x 10..360, y 80..110.
    assert match == {"page_number": 1, "polygon": [[10, 80], [360, 80], [360, 110], [10, 110]]}


def test_match_is_case_and_punctuation_insensitive() -> None:
    assert locate_value(GEOMETRY, "mawdsley-brooks ltd.") is not None


def test_value_not_on_the_page_returns_none() -> None:
    assert locate_value(GEOMETRY, "NOT-PRESENT-99") is None
    assert locate_value(GEOMETRY, "   ") is None


def test_page_hint_is_searched_first() -> None:
    # "8077219" is on both pages; the hint decides which one wins.
    assert locate_value(GEOMETRY, "8077219", page_hint=2)["page_number"] == 2
    assert locate_value(GEOMETRY, "8077219", page_hint=1)["page_number"] == 1


def test_malformed_geometry_is_safe() -> None:
    assert locate_value({}, "x") is None
    assert locate_value({"pages": "nope"}, "x") is None
    assert locate_value({"pages": [{"spans": [{"text": 5, "polygon": "bad"}]}]}, "x") is None
